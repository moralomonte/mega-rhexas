# Overlap one download with one search. Equivalent:
#   rhexas queue --sectors 107,106,105 --predownload 104 --wait-pid <pid>
# Pass -Rerun to skip sectors that already have .download.done and to vet afterwards.
param(
  [int]$WaitPid = 0,
  [int[]]$PreDownload = @(),
  [int[]]$Queue = @(),
  [double]$MaxDataGB = 0,
  [double]$MinFreeGB = 0,
  [switch]$Rerun
)
if (-not $Queue -or $Queue.Count -eq 0) {
  Write-Error "Pass -Queue 107,106,105 (or run: rhexas queue --sectors 107,106,105)"
  exit 2
}
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$rargs = @("queue", "--sectors", ($Queue -join ","))
if ($PreDownload -and $PreDownload.Count -gt 0) { $rargs += @("--predownload", ($PreDownload -join ",")) }
if ($WaitPid) { $rargs += @("--wait-pid", "$WaitPid") }
if ($PSBoundParameters.ContainsKey("MaxDataGB")) { $rargs += @("--max-project-gb", "$MaxDataGB") }
if ($PSBoundParameters.ContainsKey("MinFreeGB")) { $rargs += @("--min-free-gb", "$MinFreeGB") }
if ($Rerun) { $rargs += "--rerun" }
$exe = Join-Path $root "venv\Scripts\rhexas.exe"
if (Test-Path $exe) { & $exe @rargs }
else {
  $py = Join-Path $root "venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py -m rhexas @rargs
}
exit $LASTEXITCODE
