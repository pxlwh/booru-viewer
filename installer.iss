; booru-viewer Windows Installer

[Setup]
AppName=booru-viewer
AppVersion=0.1.7
AppPublisher=pax
AppPublisherURL=https://git.pax.moe/pax/booru-viewer
DefaultDirName={localappdata}\booru-viewer
DefaultGroupName=booru-viewer
OutputBaseFilename=booru-viewer-setup
OutputDir=dist
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\booru-viewer.exe
PrivilegesRequired=lowest

[Files]
Source: "dist\booru-viewer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\booru-viewer"; Filename: "{app}\booru-viewer.exe"
Name: "{autodesktop}\booru-viewer"; Filename: "{app}\booru-viewer.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\booru-viewer.exe"; Description: "Launch booru-viewer"; Flags: nowait postinstall skipifsilent
