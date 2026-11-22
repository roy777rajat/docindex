# Deploy Phase 7: dispatcher Lambda, gate schedule and the two Step Functions workflows.
# Run from the repo root in PowerShell:
#   .\scripts\deploy-orchestration.ps1 -UrlTemplate "https://cleandocs-{service}-service.onrender.com"
# {service} is replaced with: validation archive manifest normalization cleaning duplicate extraction metadata enrichment
#                             chunking embedding vector-indexing entity-extraction graph
# The services do not need to exist yet; the workflows only call them when an ingestion runs.
param(
  [string]$UrlTemplate = "https://cleandocs-{service}-service.onrender.com",
  [string]$UrlsFile = "",   # render-urls.json from scripts\render-urls.ps1 (use when Render added suffixes to the URLs)
  [string]$Region = "eu-west-1",
  [string]$StackName = "cleandocs-orchestration",
  [string]$ServiceAuthSecret = $env:SERVICE_AUTH_SECRET
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $ServiceAuthSecret -or $ServiceAuthSecret.Length -lt 24) {
  throw "Set SERVICE_AUTH_SECRET (24+ chars) in the environment or pass -ServiceAuthSecret. It must equal the value on the Render services."
}

Write-Host "1/5 core stack (IAM updates for kb-source delete and KB document listing)"
$admin = aws sts get-caller-identity --query Arn --output text
aws cloudformation deploy --template-file infrastructure/cleandocs-core.yaml --stack-name cleandocs-core `
  --capabilities CAPABILITY_NAMED_IAM --tags project=cleandocs --region $Region --parameter-overrides "AdminPrincipalArn=$admin"
if ($LASTEXITCODE -ne 0) { throw "core stack deploy failed" }

Write-Host "2/5 generate workflow definitions"
if ($UrlsFile) { python workflows/build.py --urls-file $UrlsFile } else { python workflows/build.py --url-template $UrlTemplate }
if ($LASTEXITCODE -ne 0) { throw "workflow build failed" }

Write-Host "3/5 package dispatcher"
$pkg = python scripts/package_dispatcher.py
$zip = $pkg[0]; $hash = $pkg[1]
$account = aws sts get-caller-identity --query Account --output text
$bucket = "cleandocs-$account-$Region"
$ingHash = (Get-FileHash infrastructure/generated/ingestion.asl.json -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$fileHash = (Get-FileHash infrastructure/generated/file.asl.json -Algorithm SHA256).Hash.Substring(0, 12).ToLower()

Write-Host "4/5 upload to s3://$bucket/deploy/ (content-hashed keys so changes redeploy)"
aws s3 cp $zip "s3://$bucket/deploy/dispatcher-$hash.zip" --region $Region
aws s3 cp infrastructure/generated/ingestion.asl.json "s3://$bucket/deploy/ingestion-$ingHash.asl.json" --region $Region
aws s3 cp infrastructure/generated/file.asl.json "s3://$bucket/deploy/file-$fileHash.asl.json" --region $Region

Write-Host "5/5 orchestration stack"
aws cloudformation deploy --template-file infrastructure/cleandocs-orchestration.yaml --stack-name $StackName `
  --capabilities CAPABILITY_NAMED_IAM --tags project=cleandocs --region $Region --parameter-overrides `
  "ServiceAuthSecret=$ServiceAuthSecret" "DefinitionBucket=$bucket" "DispatcherCodeKey=deploy/dispatcher-$hash.zip" `
  "IngestionDefinitionKey=deploy/ingestion-$ingHash.asl.json" "FileDefinitionKey=deploy/file-$fileHash.asl.json"
if ($LASTEXITCODE -ne 0) { throw "orchestration stack deploy failed" }

aws cloudformation describe-stacks --stack-name $StackName --region $Region --query "Stacks[0].Outputs" --output table
Write-Host "Set STEP_FUNCTIONS_INGESTION_ARN on upload-service to the IngestionStateMachineArn above."
