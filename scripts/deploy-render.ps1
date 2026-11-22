# Trigger a manual deploy of the latest GitHub commit for some or all cleandocs-*-service web services, then wait until each is live.
# autoDeploy is off on purpose, so pushing code to GitHub does NOT update Render; run this (or use Manual Deploy in the dashboard).
#   $env:RENDER_API_KEY = "rnd_..."          (Account Settings -> API Keys; delete the key when you are done)
#   .\scripts\deploy-render.ps1                          # all 16
#   .\scripts\deploy-render.ps1 -Only reconciliation     # one service (name without the cleandocs- and -service parts)
#   .\scripts\deploy-render.ps1 -Only upload,manifest -NoWait
param([string[]]$Only = @(), [switch]$NoWait, [string]$ApiKey = $env:RENDER_API_KEY, [int]$TimeoutMinutes = 20)
$ErrorActionPreference = "Stop"
if (-not $ApiKey) { throw "Set RENDER_API_KEY or pass -ApiKey" }
$h = @{ Authorization = "Bearer $ApiKey"; Accept = "application/json" }
$api = "https://api.render.com/v1"

$services = @(); $cursor = $null
do {
  $uri = "$api/services?limit=100" + $(if ($cursor) { "&cursor=$cursor" } else { "" })
  $page = Invoke-RestMethod -Headers $h -Uri $uri
  foreach ($i in $page) {
    if ($i.service.name -match '^cleandocs-(.+)-service$') { $services += [pscustomobject]@{ Key = $Matches[1]; Id = $i.service.id; Name = $i.service.name } }
    $cursor = $i.cursor
  }
} while ($page.Count -eq 100)
if ($Only.Count) {
  $unknown = $Only | Where-Object { $_ -notin $services.Key }
  if ($unknown) { throw "unknown service(s): $($unknown -join ', '). Known: $($services.Key -join ', ')" }
  $services = $services | Where-Object { $_.Key -in $Only }
}
$started = @()
foreach ($s in $services) {
  $d = Invoke-RestMethod -Method Post -Headers $h -Uri "$api/services/$($s.Id)/deploys" -ContentType "application/json" -Body "{}"
  $deployId = if ($d.id) { $d.id } else { $d.deploy.id }
  Write-Host ("started  {0,-34} deploy {1}" -f $s.Name, $deployId)
  $started += [pscustomobject]@{ Name = $s.Name; ServiceId = $s.Id; DeployId = $deployId }
}
if ($NoWait) { exit 0 }
$deadline = (Get-Date).AddMinutes($TimeoutMinutes); $pending = @($started); $failed = @()
while ($pending.Count -and (Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 20
  $still = @()
  foreach ($p in $pending) {
    $d = Invoke-RestMethod -Headers $h -Uri "$api/services/$($p.ServiceId)/deploys/$($p.DeployId)"
    $st = $d.status; $commit = if ($d.commit) { $d.commit.id.Substring(0, 7) } else { "?" }
    if ($st -eq "live") { Write-Host ("live     {0,-34} commit {1}" -f $p.Name, $commit) }
    elseif ($st -in @("build_failed", "update_failed", "canceled", "pre_deploy_failed")) { Write-Host ("FAILED   {0,-34} {1}" -f $p.Name, $st); $failed += $p.Name }
    else { $still += $p }
  }
  $pending = $still
}
if ($pending.Count) { Write-Host "still running after $TimeoutMinutes min: $($pending.Name -join ', ')"; exit 2 }
if ($failed.Count) { Write-Host "`nfailed: $($failed -join ', ') - open the service in Render and read the deploy log"; exit 1 }
Write-Host "`nall deployed. Check readiness: Get-Content render-urls.json | ConvertFrom-Json | ForEach-Object { `$_.PSObject.Properties } | ForEach-Object { curl.exe -s `"`$(`$_.Value)/health/ready`" }"
