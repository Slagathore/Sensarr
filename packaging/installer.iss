; Plexxarr Windows installer (Inno Setup 6)
;
; Wraps the PyInstaller onedir bundle (the same folder zipped into
; Plexxarr-<ver>-windows-x64.zip) in a per-user installer with Start Menu
; shortcuts and a clean uninstaller. This does NOT rebuild the app -- point
; SourceDir at an already-built (and already-signed, if you're doing a real
; release) bundle folder that contains Plexxarr.exe directly.
;
; Required command-line defines (ISCC /D...):
;   AppVersion  e.g. 1.2          -- must match config.py APP_VERSION
;   SourceDir   e.g. C:\...\dist\20260713-...\Plexxarr   -- the onedir bundle
;                (the folder that directly contains Plexxarr.exe)
;
; Example:
;   ISCC.exe /DAppVersion=1.2 /DSourceDir="C:\path\to\dist\...\Plexxarr" ^
;            packaging\installer.iss
;
; Output: Plexxarr-<AppVersion>-Setup.exe in packaging\Output\

#ifndef AppVersion
  #error AppVersion is required, e.g. ISCC /DAppVersion=1.2 installer.iss
#endif

#ifndef SourceDir
  #error SourceDir is required, e.g. ISCC /DSourceDir="C:\path\to\dist\...\Plexxarr" installer.iss
#endif

#define AppName "Plexxarr"
#define AppPublisher "Charles Chambers"
#define AppURL "https://github.com/Slagathore/Plexxarr"
#define AppExeName "Plexxarr.exe"

[Setup]
AppId={{8F2B6C7A-6E3D-4C6E-9C0E-3B7E6F1A9D2A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename={#AppName}-{#AppVersion}-Setup
SetupIconFile=..\assets\plexxarr.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[UninstallDelete]
; The app writes its own runtime state (sqlite db, pid file, caches) directly
; into {app} on first run -- Inno Setup's default uninstall only removes files
; it explicitly installed, so those would otherwise survive as an orphaned
; directory. This is a per-user install with its own dedicated folder, so wipe
; it entirely on uninstall.
Type: filesandordirs; Name: "{app}"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent unchecked
