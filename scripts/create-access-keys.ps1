# Create one access key per AWS tier and write Render-ready KEY=VALUE files into .\secrets\ (git-ignored).
# Run once from the repo root:  .\scripts\create-access-keys.ps1
# In Render: Environment Groups -> New group -> "Add from .env" -> paste the file content. Then DELETE .\secrets\.
# Keys can be revoked any time:  aws iam delete-access-key --user-name <user> --access-key-id <id>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force secrets | Out-Null
$tiers = [ordered]@{ ingest = "cleandocs-ingest"; processing = "cleandocs-processing"; indexing = "cleandocs-indexing" }
foreach ($t in $tiers.Keys) {
  $user = $tiers[$t]
  $out = "secrets\aws-$t.env"
  if (Test-Path $out) { Write-Host "skip $t : $out already exists"; continue }
  $count = (aws iam list-access-keys --user-name $user --query "length(AccessKeyMetadata)" --output text)
  if ([int]$count -ge 2) { throw "$user already has 2 access keys (AWS limit). Delete an unused one first." }
  $k = aws iam create-access-key --user-name $user --query AccessKey --output json | ConvertFrom-Json
  "AWS_ACCESS_KEY_ID=$($k.AccessKeyId)`nAWS_SECRET_ACCESS_KEY=$($k.SecretAccessKey)`n" | Set-Content -Path $out -Encoding ascii -NoNewline
  Write-Host "created $out  (user $user, key $($k.AccessKeyId))"
}
Write-Host "`nPaste each file into its Render env group, then delete the secrets folder:  Remove-Item -Recurse -Force secrets"
