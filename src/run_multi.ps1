# Several sectors, one after another. Disk limits come from rhexas.toml unless -MaxDataGB / -MinFreeGB are passed.
# Equivalent: rhexas run --sectors 106,105
param([int[]]$Sectors = @(106, 105), [string]$DataDir = "", [double]$MaxDataGB = 0, [double]$MinFreeGB = 0)
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$rargs = @("run", "--sectors", ($Sectors -join ","))
if ($PSBoundParameters.ContainsKey("DataDir") -and $DataDir) { $rargs += @("--data-dir", $DataDir) }
if ($PSBoundParameters.ContainsKey("MaxDataGB")) { $rargs += @("--max-project-gb", "$MaxDataGB") }
if ($PSBoundParameters.ContainsKey("MinFreeGB")) { $rargs += @("--min-free-gb", "$MinFreeGB") }
$exe = Join-Path $root "venv\Scripts\rhexas.exe"
if (Test-Path $exe) { & $exe @rargs }
else {
  $py = Join-Path $root "venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py -m rhexas @rargs
}
exit $LASTEXITCODE
