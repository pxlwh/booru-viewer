; booru-viewer Windows Installer

[Setup]
AppName=booru-viewer
AppVersion=0.2.5
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

[Code]
var
  RemoveDataCheckbox: TNewCheckBox;

procedure InitializeUninstallProgressForm();
var
  UninstallPage: TNewStaticText;
begin
  RemoveDataCheckbox := TNewCheckBox.Create(UninstallProgressForm);
  RemoveDataCheckbox.Parent := UninstallProgressForm;
  RemoveDataCheckbox.Left := 10;
  RemoveDataCheckbox.Top := UninstallProgressForm.ClientHeight - 50;
  RemoveDataCheckbox.Width := UninstallProgressForm.ClientWidth - 20;
  RemoveDataCheckbox.Height := 20;
  RemoveDataCheckbox.Caption := 'REMOVE ALL USER DATA (BOOKMARKS, CACHE, LIBRARY — DATA LOSS)';
  RemoveDataCheckbox.Font.Color := clRed;
  RemoveDataCheckbox.Font.Style := [fsBold];
  RemoveDataCheckbox.Checked := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveDataCheckbox.Checked then
    begin
      AppDataDir := ExpandConstant('{userappdata}\booru-viewer');
      if DirExists(AppDataDir) then
        DelTree(AppDataDir, True, True, True);
    end;
  end;
end;
