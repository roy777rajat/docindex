# Read the real public URLs of the cleandocs-*-service web services from Render and write render-urls.json.
# Render appends a suffix to a service URL when the name is already taken, so never assume the URL.
#   $env:RENDER_API_KEY = "rnd_..."   (Render dashboard -> Account Settings -> API Keys)
#   .\scripts\render-urls.ps1
param([string]$ApiKey = $env:RENDER_API_KEY, [string]$Out = "render-urls.json")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $ApiKey) { throw "Set RENDER_API_KEY or pass -ApiKey" }
$map = [ordered]@{}; $cursor = $null
do {
  $uri = "https://api.render.com/v1/services?limit=100" + $(if ($cursor) { "&cursor=$cursor" } else { "" })
  $page = Invoke-RestMethod -Headers @{ Authorization = "Bearer $ApiKey" } -Uri $uri
  foreach ($i in $page) {
    $s = $i.service
    if ($s.name -match '^cleandocs-(.+)-service$') { $map[$Matches[1]] = $s.serviceDetails.url }
    $cursor = $i.cursor
  }
} while ($page.Count -eq 100)
$map | ConvertTo-Json | Set-Content -Path $Out -Encoding utf8
$map.GetEnumerator() | ForEach-Object { "{0,-18} {1}" -f $_.Key, $_.Value }
Write-Host "`nwrote $Out ($($map.Count) services). Next: .\scripts\deploy-orchestration.ps1 -UrlsFile $Out"
