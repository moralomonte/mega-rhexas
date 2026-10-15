# Build the per-user Windows installer.
# Embedded CPython, then pip install of this repo (including the desktop extra).
# A real interpreter is what numpy, astropy, and lightkurve expect. PyInstaller
# regularly drops their data files. The same `rhexas desktop` command is what a
# later macOS bundle should launch; this script is the Windows half only.
$ErrorActionPreference = "Stop"

$PythonVersion = if ($env:RHEXAS_EMBED_PYTHON) { $env:RHEXAS_EMBED_PYTHON } else { "3.12.10" }
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$App = Join-Path $Here "dist\app"
$Stage = Join-Path $Here "embed"

if (Test-Path $App) { Remove-Item -Recurse -Force $App }
New-Item -ItemType Directory -Force -Path $App, $Stage | Out-Null

$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $Stage $zipName
if (-not (Test-Path $zipPath)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zipPath
}
Expand-Archive -Path $zipPath -DestinationPath $App -Force

$parts = $PythonVersion.Split(".")
$tag = "$($parts[0])$($parts[1])"
$pth = Join-Path $App "python$tag._pth"
if (-not (Test-Path $pth)) { throw "embeddable layout has no $pth" }
@(
    "python$tag.zip"
    "."
    "Lib\site-packages"
    "import site"
) | Set-Content -Path $pth -Encoding ascii

$getPip = Join-Path $Stage "get-pip.py"
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}
& (Join-Path $App "python.exe") $getPip
if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }

$python = Join-Path $App "python.exe"
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
# The embeddable ._pth ignores PYTHONPATH, so an isolated build env cannot see
# its own setuptools. Install the backend here and build without isolation.
& $python -m pip install "setuptools>=68" wheel
if ($LASTEXITCODE -ne 0) { throw "setuptools install failed" }
& $python -m pip install --no-build-isolation ($Root + "[desktop]")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& $python -c "import numpy, astropy, lightkurve, rhexas, pystray; from PIL import Image; print('bundled', rhexas.__version__)"
if ($LASTEXITCODE -ne 0) { throw "bundled imports failed" }
if (-not (Test-Path (Join-Path $App "pythonw.exe"))) { throw "pythonw.exe missing" }

Copy-Item (Join-Path $Here "..\assets\triskelion.ico") (Join-Path $App "triskelion.ico") -Force

$version = "1.0.0"
if ($env:GITHUB_REF_NAME -match '^v(.+)$') { $version = $Matches[1] }
$iscc = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) is not installed" }

& $iscc "/DMyAppVersion=$version" (Join-Path $Here "mega-rhexas.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
Get-ChildItem (Join-Path $Here "output") -Filter "MegaRhexas-Setup-*.exe" | ForEach-Object {
    Write-Host ("installer {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
