# Stops the dashboard only (never touches the search pipeline).
# Usage: .\stop_dashboard.ps1 [-Demo] [-Sector 106]
param([switch]$Demo, [int]$Sector = 0)
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$rargs = @("dashboard", "--stop")
if ($Demo) { $rargs += "--demo" }
if ($Sector) { $rargs += @("--sector", "$Sector") }
$exe = Join-Path $root "venv\Scripts\rhexas.exe"
if (Test-Path $exe) { & $exe @rargs }
else {
  $py = Join-Path $root "venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py -m rhexas @rargs
}
exit $LASTEXITCODE
