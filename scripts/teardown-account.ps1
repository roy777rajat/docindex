# Remove CleanDocs from the CURRENT AWS account so AWS stops charging. Irreversible. Run from the repo root.
#   .\scripts\teardown-account.ps1 -Inventory     list what exists (deletes nothing)
#   .\scripts\teardown-account.ps1 -Execute       delete everything (asks you to type the account id)
# Order matters: data -> access keys -> orchestration stack -> core stack -> bucket + tables (retained by the stack) -> leftovers.
# Render is separate: delete the services/env groups in the Render dashboard (see docs/aws-teardown.md). NOT tested end to end.
param([string]$Region = "eu-west-1", [switch]$Inventory, [switch]$Execute)
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONIOENCODING = "utf-8"
$acct = aws sts get-caller-identity --query Account --output text
if (-not $acct) { throw "AWS credentials not working" }
$bucket = "cleandocs-$acct-$Region"
$users = "cleandocs-ingest", "cleandocs-processing", "cleandocs-indexing"

function Show-Inventory {
  Write-Host "`n=== Account $acct / $Region ==="
  Write-Host "`n[CloudFormation stacks]"; aws cloudformation list-stacks --region $Region --query "StackSummaries[?starts_with(StackName,'cleandocs') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" --output table
  Write-Host "[S3 bucket $bucket (retained by the stack, must be emptied and deleted by hand)]"; aws s3 ls "s3://$bucket" --summarize --human-readable --recursive --region $Region 2>&1 | Select-Object -Last 2
  Write-Host "[DynamoDB tables]"; aws dynamodb list-tables --region $Region --query "TableNames[?contains(@,'cleandocs') || contains(@,'document-ingest')]" --output text
  Write-Host "[Vector bucket]"; aws s3vectors list-vector-buckets --region $Region --query "vectorBuckets[?contains(vectorBucketName,'cleandocs')].vectorBucketName" --output text 2>&1
  Write-Host "[Bedrock knowledge bases]"; aws bedrock-agent list-knowledge-bases --region $Region --query "knowledgeBaseSummaries[].[name,knowledgeBaseId,status]" --output table
  Write-Host "[Step Functions]"; aws stepfunctions list-state-machines --region $Region --query "stateMachines[?starts_with(name,'cleandocs')].name" --output text
  Write-Host "[Lambda]"; aws lambda list-functions --region $Region --query "Functions[?starts_with(FunctionName,'cleandocs')].FunctionName" --output text
  Write-Host "[SQS]"; aws sqs list-queues --region $Region --queue-name-prefix cleandocs --output text
  Write-Host "[IAM users and access keys]"; foreach ($u in $users) { "{0}: {1} key(s)" -f $u, (aws iam list-access-keys --user-name $u --query "length(AccessKeyMetadata)" --output text 2>$null) }
  Write-Host "[Log groups]"; aws logs describe-log-groups --region $Region --query "logGroups[?contains(logGroupName,'cleandocs')].logGroupName" --output text
  Write-Host "[Secrets Manager (EventBridge connection secret; deleted with the stack)]"; aws secretsmanager list-secrets --region $Region --query "SecretList[?contains(Name,'cleandocs')].Name" --output text
  Write-Host "`nEverything tagged project=cleandocs:"; aws resourcegroupstaggingapi get-resources --region $Region --tag-filters Key=project,Values=cleandocs --query "ResourceTagMappingList[].ResourceARN" --output text
}

if ($Inventory -or -not $Execute) { Show-Inventory; if (-not $Execute) { Write-Host "`nTo delete all of this: .\scripts\teardown-account.ps1 -Execute"; return } }

$c = Read-Host "This DELETES all CleanDocs data and infrastructure in account $acct. Type the account id to continue"
if ($c -ne $acct) { Write-Host "Aborted."; return }

Write-Host "`n1/7 stop running workflows"
foreach ($sm in (aws stepfunctions list-state-machines --region $Region --query "stateMachines[?starts_with(name,'cleandocs')].stateMachineArn" --output text).Split()) {
  if ($sm) { foreach ($e in (aws stepfunctions list-executions --state-machine-arn $sm --status-filter RUNNING --region $Region --query "executions[].executionArn" --output text).Split()) { if ($e) { aws stepfunctions stop-execution --execution-arn $e --region $Region | Out-Null } } }
}
Write-Host "2/7 delete all document data + Knowledge Base vectors"
uv run --python 3.12 --with "boto3>=1.40" python scripts\clean_all.py --region $Region --yes "DELETE-ALL-$acct"
Write-Host "3/7 delete IAM access keys (a user with keys cannot be deleted by the stack)"
foreach ($u in $users) { foreach ($k in (aws iam list-access-keys --user-name $u --query "AccessKeyMetadata[].AccessKeyId" --output text 2>$null).Split()) { if ($k) { aws iam delete-access-key --user-name $u --access-key-id $k } } }
Write-Host "4/7 delete orchestration stack"
aws cloudformation delete-stack --stack-name cleandocs-orchestration --region $Region
aws cloudformation wait stack-delete-complete --stack-name cleandocs-orchestration --region $Region
Write-Host "5/7 delete core stack (KB, vector index, queues, IAM users and roles)"
aws cloudformation delete-stack --stack-name cleandocs-core --region $Region
aws cloudformation wait stack-delete-complete --stack-name cleandocs-core --region $Region
Write-Host "6/7 empty and delete the S3 bucket and the two DynamoDB tables (the stack retained them)"
uv run --python 3.12 --with boto3 python scripts\purge_bucket.py $bucket --region $Region --yes
aws s3api delete-bucket --bucket $bucket --region $Region
aws dynamodb delete-table --table-name cleandocs-registry --region $Region | Out-Null
aws dynamodb delete-table --table-name document-ingest-trail --region $Region | Out-Null
Write-Host "7/7 leftovers"
foreach ($g in (aws logs describe-log-groups --region $Region --query "logGroups[?contains(logGroupName,'cleandocs')].logGroupName" --output text).Split()) { if ($g) { aws logs delete-log-group --log-group-name $g --region $Region } }
Show-Inventory
Write-Host "`nDone. Anything still listed above must be deleted by hand. Check AWS Billing -> Cost Explorer tomorrow to confirm no new charges."
Write-Host "Also: delete the Render services/env groups and the Render API key (docs/aws-teardown.md, section 'Render and GitHub')."
