# Stop the local server started by run_local.ps1 (also kills the child python that uv launches).
$root = Split-Path -Parent $PSScriptRoot
$f = Join-Path $root ".local-pids.json"
if (-not (Test-Path $f)) { Write-Host "nothing to stop"; exit 0 }
$id = [int](Get-Content $f -Raw)
& taskkill /PID $id /T /F 2>$null | Out-Null
Remove-Item $f
Write-Host "stopped"
