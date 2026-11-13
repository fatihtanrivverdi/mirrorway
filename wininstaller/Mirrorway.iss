; Mirrorway — the Windows installer (Inno Setup 6). Built by scripts/build_windows_installer.py:
;   ISCC /DMyVersion=3.1.5 /DSourceDir=<staged folder> /DOutputDir=<out> wininstaller\Mirrorway.iss
; The staged folder holds a private Python (python\, with the packages from requirements-windows.txt) and the code
; (mirrorway\). Installs for the current user into %LOCALAPPDATA%\Mirrorway without administrator rights, replaces
; an earlier Mirrorway (or Köprü) installation and starts Mirrorway. Settings in %USERPROFILE%\.mirrorway stay.

#ifndef MyVersion
  #error Pass /DMyVersion=<version>
#endif
#ifndef SourceDir
  #error Pass /DSourceDir=<staged folder>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{8C5B5E2A-3F41-4C3B-9E0D-6A1F2B7C4D90}
AppName=Mirrorway
AppVersion={#MyVersion}
AppVerName=Mirrorway {#MyVersion}
AppPublisher=Fatih Tanrıverdi
AppPublisherURL=https://github.com/fatihtanrivverdi/mirrorway
AppSupportURL=https://github.com/fatihtanrivverdi/mirrorway
VersionInfoVersion={#MyVersion}
DefaultDirName={localappdata}\Mirrorway
UsePreviousAppDir=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; one click: no questions; Mirrorway starts when the installation ends
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
ShowLanguageDialog=no
CloseApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=Mirrorway-{#MyVersion}-windows-setup
SetupIconFile={#SourceDir}\mirrorway\assets\mirrorway.ico
UninstallDisplayIcon={app}\mirrorway\assets\mirrorway.ico
UninstallDisplayName=Mirrorway
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

[InstallDelete]
; the previous code and Python are replaced as a whole (no stale files); the virtual environment of the old installer
Type: filesandordirs; Name: "{app}\mirrorway"
Type: filesandordirs; Name: "{app}\mirrorway.old"
Type: filesandordirs; Name: "{app}\_update"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\.venv"
; Köprü, the app's former name: its program folder and shortcuts (its settings in .kopru are imported on first start)
Type: filesandordirs; Name: "{localappdata}\Kopru"
Type: files; Name: "{userprograms}\Köprü.lnk"
Type: files; Name: "{userprograms}\Kopru.lnk"
Type: files; Name: "{userdesktop}\Köprü.lnk"
Type: files; Name: "{userdesktop}\Kopru.lnk"
Type: files; Name: "{userstartup}\Köprü.lnk"
Type: files; Name: "{userstartup}\Kopru.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Mirrorway"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mirrorway.win"; WorkingDir: "{app}"; IconFilename: "{app}\mirrorway\assets\mirrorway.ico"; Comment: "Use the Mac's keyboard and mouse here and see the Mac's screen"
Name: "{userdesktop}\Mirrorway"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mirrorway.win"; WorkingDir: "{app}"; IconFilename: "{app}\mirrorway\assets\mirrorway.ico"; Check: WantDesktop
Name: "{userstartup}\Mirrorway"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m mirrorway.win"; WorkingDir: "{app}"; IconFilename: "{app}\mirrorway\assets\mirrorway.ico"; Check: WantStartup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-m mirrorway.win"; WorkingDir: "{app}"; Flags: nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: files; Name: "{userstartup}\Mirrorway.lnk"

[Code]
var
  PrevInstalled, HadStartup, HadDesktop: Boolean;

function InitializeSetup(): Boolean;
begin
  // an update keeps the user's choices: a removed desktop shortcut or a turned-off start with Windows stay so
  PrevInstalled := DirExists(ExpandConstant('{localappdata}\Mirrorway\mirrorway'));
  HadStartup := FileExists(ExpandConstant('{userstartup}\Mirrorway.lnk'));
  HadDesktop := FileExists(ExpandConstant('{userdesktop}\Mirrorway.lnk'));
  Result := True;
end;

function WantStartup(): Boolean;
begin
  Result := (not PrevInstalled) or HadStartup;
end;

function WantDesktop(): Boolean;
begin
  Result := (not PrevInstalled) or HadDesktop;
end;

function PowerShell(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

procedure StopMirrorway();
var
  Code: Integer;
begin
  // a running Mirrorway (or Köprü) holds its files open; this PowerShell's own command line matches too: skip it
  Exec(PowerShell(), '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | ' +
    'Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and ($_.CommandLine -like ''*mirrorway.win*'' -or ' +
    '$_.CommandLine -like ''*kopru.win*'') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force ' +
    '-ErrorAction SilentlyContinue }"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(700);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopMirrorway();
  Result := '';
end;

procedure AddFirewallRule();
var
  Code: Integer;
begin
  // so that the Mac is found automatically (UDP 24851); asks for administrator permission once, skipped if refused
  if Exec(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall show rule name="Mirrorway discovery"', '',
          SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    Exit;
  ShellExec('runas', PowerShell(), '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "' +
    'New-NetFirewallRule -DisplayName ''Mirrorway discovery'' -Direction Inbound -Action Allow -Protocol UDP ' +
    '-LocalPort 24851 -Profile Any | Out-Null"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent()) then
    AddFirewallRule();
end;

procedure RemoveLockScreenSupport();
var
  Code: Integer;
begin
  { Lock screen support is a service (installed from Mirrorway's Settings with administrator permission): it goes
    too, with one administrator prompt }
  if RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\MirrorwaySecure') then
    ShellExec('runas', ExpandConstant('{app}\python\python.exe'), '-m mirrorway.win.svcinstall uninstall',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
end;

function InitializeUninstall(): Boolean;
begin
  StopMirrorway();
  RemoveLockScreenSupport();
  Result := True;
end;
