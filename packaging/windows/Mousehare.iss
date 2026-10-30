#define AppName "Mousehare"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppPublisher "Mousehare"
#define AppExeName "Mousehare.exe"

[Setup]
AppId={{A0A5A3F8-0142-4C05-8E7C-7E9EE4EA4685}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Mousehare
DefaultGroupName=Mousehare
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=..\..\release
OutputBaseFilename=Mousehare-Setup-{#AppVersion}
SetupIconFile=..\..\.build\icons\mouseshare.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
; SignTool=signtool sign /a /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\Mousehare\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Mousehare"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Mousehare"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch Mousehare"; Flags: postinstall nowait skipifsilent
