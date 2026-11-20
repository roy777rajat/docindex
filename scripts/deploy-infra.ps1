# Create or update the core AWS stack. Run from repo root in PowerShell:  .\scripts\deploy-infra.ps1
# Uses your default AWS credentials/profile. Region defaults to eu-west-1.
param([string]$Region = "eu-west-1", [string]$StackName = "cleandocs-core")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

aws cloudformation deploy `
  --template-file infrastructure/cleandocs-core.yaml `
  --stack-name $StackName `
  --capabilities CAPABILITY_NAMED_IAM `
  --tags project=cleandocs `
  --region $Region

aws cloudformation describe-stacks --stack-name $StackName --region $Region `
  --query "Stacks[0].Outputs" --output table
