; Tonewright installer. Build it with installer\build_installer.ps1 (it stages the files first).
#ifndef AppVersion
  #define AppVersion "1.3.0"
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
; Installs into C:\Program Files\Tonewright (one UAC prompt). Only an administrator can change files there, so
; other programs running as the user cannot swap the app's Python or code for something else. Settings, favourites
; and the log stay in the user's AppData\Roaming\Tonewright, because Program Files is read-only for normal use.
; "/CURRENTUSER" on the command line still gives a per-user install without a prompt (used by the build's own test).
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
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
english.OldPerUserCopy=An older per-user copy of Tonewright is installed for your account. After this setup finishes, remove it in Settings > Apps so only the Program Files copy remains. Your settings are kept.
ukrainian.OldPerUserCopy=Для вашого облікового запису встановлено стару копію Tonewright. Після завершення встановлення видаліть її в Параметри > Програми, щоб лишилася тільки копія в Program Files. Ваші налаштування збережуться.

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
; AppUserModelID is the same id the program gives itself at start-up (tonewright\app.py). It is what ties the taskbar
; button of the running window to these shortcuts: pinned to the taskbar or not, the button shows Tonewright's icon and
; starts Tonewright. Without it Windows only sees pythonw.exe, and pins that program (the Python logo, and nothing
; happens when it is clicked).
Name: "{autoprograms}\Tonewright"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; IconFilename: "{app}\Tonewright.ico"; AppUserModelID: "Tonewright.AudioWorkbench"; Comment: "Audio converter, editor and audiobook maker"
Name: "{autoprograms}\{cm:ThirdPartyNotices}"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{autodesktop}\Tonewright"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; IconFilename: "{app}\Tonewright.ico"; AppUserModelID: "Tonewright.AudioWorkbench"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m tonewright"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,Tonewright}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python writes __pycache__ folders at run time; remove whatever is left of the program folder.
Type: filesandordirs; Name: "{app}"

[Code]
// An earlier per-user install is only reported, never run: an elevated setup must not execute a program from a
// folder that the user (or anything running as the user) can write to.
procedure InitializeWizard();
begin
  if IsAdminInstallMode and
     RegKeyExists(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B0F2D4E-3A5C-4E67-9C1D-7A2E58B4F0A1}_is1') then
    MsgBox(CustomMessage('OldPerUserCopy'), mbInformation, MB_OK);
end;

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
