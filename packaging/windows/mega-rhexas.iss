; Per-user Mega Rhexas installer. No administrator password.
; "Start when I sign in" is a task and is unchecked unless the person opts in.
; Uninstall does not delete %LOCALAPPDATA%\Mega Rhexas (token and cache).

#ifndef MyAppVersion
  #define MyAppVersion "0.3.5"
#endif

#define MyAppName "Mega Rhexas"
#define MyAppPublisher "Mega Rhexas"
#define MyAppURL "https://github.com/moralomonte/mega-rhexas"

[Setup]
AppId={{7C4E9A2B-1F6D-4E8A-9C3B-2D5F8A1E6B40}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\Mega Rhexas
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=MegaRhexas-Setup-{#MyAppVersion}
SetupIconFile=..\assets\triskelion.ico
WizardImageFile=..\assets\wizard-image.bmp
WizardSmallImageFile=..\assets\wizard-small.bmp
; Site background #05060f, so the images meet the wizard without a light fringe.
WizardImageBackColor=$0F0605
WizardSmallImageBackColor=$0F0605
UninstallDisplayIcon={app}\triskelion.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
CloseApplications=no

[Tasks]
Name: startup; Description: "Start Mega Rhexas when I sign in"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "dist\app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\triskelion.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Mega Rhexas"; Filename: "{app}\pythonw.exe"; Parameters: "-m rhexas desktop"; WorkingDir: "{app}"; IconFilename: "{app}\triskelion.ico"
Name: "{userstartup}\Mega Rhexas"; Filename: "{app}\pythonw.exe"; Parameters: "-m rhexas desktop"; WorkingDir: "{app}"; IconFilename: "{app}\triskelion.ico"; Tasks: startup

[Run]
Filename: "{app}\pythonw.exe"; Parameters: "-m rhexas desktop"; Description: "Start Mega Rhexas"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent
