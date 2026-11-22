# Wake the sleeping Render Free services BEFORE you upload (they sleep after ~15 minutes idle and take ~1 minute to wake).
#   .\scripts\warm-up.ps1                 wake all services, wait until each answers
#   .\scripts\warm-up.ps1 -Only upload,manifest
# Uses render-urls.json (from scripts\render-urls.ps1) or the default URL pattern.
param([string[]]$Only = @(), [string]$UrlsFile = "render-urls.json", [int]$TimeoutSeconds = 240)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$names = "upload","validation","archive","manifest","normalization","cleaning","duplicate","extraction","metadata","enrichment","chunking","embedding","vector-indexing","reconciliation","entity-extraction","graph"
if ($Only.Count) { $names = $names | Where-Object { $Only -contains $_ } }
$urls = @{}
if (Test-Path $UrlsFile) { (Get-Content $UrlsFile -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $urls[$_.Name] = $_.Value } }
$jobs = foreach ($n in $names) {
  $u = if ($urls[$n]) { $urls[$n].TrimEnd("/") } else { "https://cleandocs-$n-service.onrender.com" }
  Start-Job -ArgumentList $n, $u, $TimeoutSeconds -ScriptBlock {
    param($n, $u, $t)
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt $t) {
      $code = & curl.exe -s -o NUL -w "%{http_code}" --max-time 70 "$u/health/live"
      if ($code -eq "200") { return "{0,-18} UP   ({1:N0}s)" -f $n, ((Get-Date) - $t0).TotalSeconds }
      Start-Sleep -Seconds 5
    }
    return "{0,-18} NOT UP after {1}s (check the Render dashboard)" -f $n, $t
  }
}
$jobs | Wait-Job | Receive-Job
$jobs | Remove-Job
