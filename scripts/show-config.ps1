# Print every value you need for Render and for the scripts, straight from AWS. Run any time:  .\scripts\show-config.ps1
param([string]$Region = "eu-west-1")
Write-Host "`n== AWS account and region"
aws sts get-caller-identity --query "{Account:Account,Arn:Arn}" --output table
Write-Host "== core stack outputs (bucket, tables, KnowledgeBaseId, DataSourceId, queues)"
aws cloudformation describe-stacks --stack-name cleandocs-core --region $Region --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
Write-Host "== orchestration stack outputs (IngestionStateMachineArn, FileStateMachineArn)"
aws cloudformation describe-stacks --stack-name cleandocs-orchestration --region $Region --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
Write-Host "== IAM users used by Render"
aws iam list-users --query "Users[?starts_with(UserName,'cleandocs-')].UserName" --output text
Write-Host "`nNot shown here (secrets are never stored in AWS output): SERVICE_AUTH_SECRET -> your own saved file / Render env group cleandocs-auth; AWS access keys -> shown once by create-access-keys.ps1"
