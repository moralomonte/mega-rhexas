# Starts dashboards via the rhexas CLI (detached). Ports come from rhexas.toml unless -Port is set.
# Usage: .\start_dashboard.ps1 [-Sector 106] [-Port 8082] [-Demo] [-NoBrowser] [-All]
param(
  [int]$Port = 0,
  [switch]$Demo,
  [switch]$NoBrowser,
  [string]$BindHost = "",
  [switch]$LocalOnly,
  [ValidateSet("eco", "live", "turbo")][string]$Mode = "",
  [int]$Sector = 0,
  [switch]$All
)
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$rargs = @("dashboard")
if ($All) { $rargs += "--all" } else { $rargs += "--detach" }
if ($Demo) { $rargs += "--demo" }
if ($NoBrowser) { $rargs += "--no-browser" }
if ($LocalOnly) { $rargs += "--local-only" }
elseif ($BindHost) { $rargs += @("--host", $BindHost) }
if ($Mode) { $rargs += @("--mode", $Mode) }
if ($Port) { $rargs += @("--port", "$Port") }
if ($Sector) { $rargs += @("--sector", "$Sector") }
$exe = Join-Path $root "venv\Scripts\rhexas.exe"
if (Test-Path $exe) { & $exe @rargs }
else {
  $py = Join-Path $root "venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py -m rhexas @rargs
}
exit $LASTEXITCODE
