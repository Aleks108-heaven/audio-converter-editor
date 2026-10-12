; Tonewright installer. Build it with installer\build_installer.ps1 (it stages the files first).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef StageDir
  #define StageDir "build\stage"
#endif

[Setup]
; Never change AppId: it is how a newer installer finds and upgrades an installed copy.
AppId={{6B0F2D4E-3A5C-4E67-9C1D-7A2E58B4F0A1}
AppName=Tonewright
AppVersion={#AppVersion}
AppVerName=Tonewright {#AppVersion}
AppPublisher=Tonewright
DefaultDirName={autopf}\Tonewright
DisableProgramGroupPage=yes
DisableDirPage=auto
; Per-user by default (no administrator prompt); the wizard offers "for all users" as a choice.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=dist
OutputBaseFilename=Tonewright-Setup-{#AppVersion}
SetupIconFile={#StageDir}\Tonewright.ico
UninstallDisplayIcon={app}\Tonewright.ico
UninstallDisplayName=Tonewright
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=auto
CloseApplications=yes
RestartApplications=no
SetupMutex=TonewrightSetupMutex
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Tonewright setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[CustomMessages]
english.RemoveSettingsPrompt=Also delete your Tonewright settings, favourites and log?%n%n(%1)%n%nChoose No to keep them for a future install.
ukrainian.RemoveSettingsPrompt=Видалити також ваші налаштування, обране та журнал Tonewright?%n%n(%1)%n%nНатисніть «Ні», щоб зберегти їх для майбутнього встановлення.
english.ThirdPartyNotices=Third-party licences
ukrainian.ThirdPartyNotices=Ліцензії сторонніх компонентів

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces these folders completely, so nothing from an older version lingers.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\site-packages"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Tonewright"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; IconFilename: "{app}\Tonewright.ico"; Comment: "Audio converter, editor and audiobook maker"
Name: "{autoprograms}\{cm:ThirdPartyNotices}"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{autodesktop}\Tonewright"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; IconFilename: "{app}\Tonewright.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,Tonewright}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python writes __pycache__ folders at run time; remove whatever is left of the program folder.
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Settings: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Settings := ExpandConstant('{userappdata}\Tonewright');
    if DirExists(Settings) and (not UninstallSilent) then
      if MsgBox(FmtMessage(CustomMessage('RemoveSettingsPrompt'), [Settings]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Settings, True, True, True);
  end;
end;
