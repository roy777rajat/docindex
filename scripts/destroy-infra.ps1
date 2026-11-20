# Delete the core stack. The S3 bucket and both DynamoDB tables have DeletionPolicy: Retain on purpose
# (they hold personal documents), so they SURVIVE stack deletion until you purge them explicitly.
#   .\scripts\destroy-infra.ps1                 -> delete stack, keep bucket and tables
#   .\scripts\destroy-infra.ps1 -PurgeData      -> also empty+delete the bucket and delete both tables (irreversible)
param([string]$Region = "eu-west-1", [string]$StackName = "cleandocs-core", [switch]$PurgeData)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

aws cloudformation delete-stack --stack-name $StackName --region $Region
aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region
Write-Host "Stack deleted. Bucket and tables retained."

if ($PurgeData) {
    $account = aws sts get-caller-identity --query Account --output text
    $bucket = "cleandocs-$account-$Region"
    $confirm = Read-Host "Type DELETE to permanently destroy $bucket and the two DynamoDB tables"
    if ($confirm -ne "DELETE") { Write-Host "Aborted."; exit 1 }
    python scripts/purge_bucket.py $bucket --region $Region --yes
    aws s3api delete-bucket --bucket $bucket --region $Region
    aws dynamodb delete-table --table-name cleandocs-registry --region $Region | Out-Null
    aws dynamodb delete-table --table-name document-ingest-trail --region $Region | Out-Null
    Write-Host "Data purged."
}
