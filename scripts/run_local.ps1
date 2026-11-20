# Start ONE local server (port 8001) hosting all 15 services under /<service-name>/...
# Usage:  .\scripts\run_local.ps1            (background; logs in logs\local\gateway.*.log)
#         .\scripts\run_local.ps1 -Foreground  (see logs live; stop with Ctrl+C)
param([switch]$Foreground, [int]$Port = 8001, [string]$Secret = "local-dev-secret-change-me-0123456789")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:SERVICE_AUTH_SECRET = $Secret
$env:LOCAL_PORT = "$Port"
if (-not $env:AWS_REGION) { $env:AWS_REGION = "eu-west-1" }

if (Get-Command uv -ErrorAction SilentlyContinue) {
  $cmd = "uv"; $args = @("run", "--python", "3.12", "--with-requirements", "requirements-local.txt", "--with", "pydantic-settings", "python", "scripts\local_gateway.py")
} else {
  $cmd = Join-Path $root ".venv\Scripts\python.exe"; $args = @("scripts\local_gateway.py")
  if (-not (Test-Path $cmd)) { throw "Install uv (https://docs.astral.sh/uv/) or run .\scripts\dev-setup.ps1 first" }
}
if ($Foreground) { & $cmd @args; exit $LASTEXITCODE }

New-Item -ItemType Directory -Force logs\local | Out-Null
$p = Start-Process -FilePath $cmd -ArgumentList $args -WorkingDirectory $root -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput logs\local\gateway.out.log -RedirectStandardError logs\local\gateway.err.log
$p.Id | Set-Content .local-pids.json
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 1
  try { Invoke-RestMethod "http://127.0.0.1:$Port/" | Out-Null; Write-Host "UP  http://127.0.0.1:$Port/   (pid $($p.Id))"; exit 0 } catch {}
  if ($p.HasExited) { break }
}
Write-Host "FAILED to start. Log:"; Get-Content logs\local\gateway.err.log -Tail 30; exit 1
