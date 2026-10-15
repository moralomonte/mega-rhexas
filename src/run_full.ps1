# Full-sector pipeline. Equivalent: rhexas run --sector 107
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$exe = Join-Path $root "venv\Scripts\rhexas.exe"
if (Test-Path $exe) { & $exe run --sector 107 }
else {
  $py = Join-Path $root "venv\Scripts\python.exe"
  if (-not (Test-Path $py)) { $py = "python" }
  & $py -m rhexas run --sector 107
}
exit $LASTEXITCODE
