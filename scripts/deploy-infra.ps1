# Create or update the core AWS stack. Run from repo root in PowerShell:  .\scripts\deploy-infra.ps1
# Uses your default AWS credentials/profile. Region defaults to eu-west-1.
param([string]$Region = "eu-west-1", [string]$StackName = "cleandocs-core", [string]$AdminPrincipalArn = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Only this IAM principal may delete objects under raw/. Default: the user you are logged in as (works in a new account too).
if (-not $AdminPrincipalArn) { $AdminPrincipalArn = aws sts get-caller-identity --query Arn --output text }
Write-Host "Admin principal: $AdminPrincipalArn"
aws cloudformation deploy `
  --template-file infrastructure/cleandocs-core.yaml `
  --stack-name $StackName `
  --capabilities CAPABILITY_NAMED_IAM `
  --tags project=cleandocs `
  --region $Region `
  --parameter-overrides "AdminPrincipalArn=$AdminPrincipalArn"

aws cloudformation describe-stacks --stack-name $StackName --region $Region `
  --query "Stacks[0].Outputs" --output table
