# Leaving the current AWS account (stop all charges)

[Back to the handbook index](README.md)

Use this when you move CleanDocs to a new AWS account (see `new-account-setup.md`) or stop the project.
**Before you delete anything**: keep your original documents (they live in `raw/` of the bucket and, hopefully, on your PC). Nothing is
migrated automatically; in a new account you upload the originals again.

## 1. What exists (the inventory)
Run `.\scripts\teardown-account.ps1 -Inventory` any time; it lists everything below and deletes nothing.

| Where | Resource | Created by | Costs money? | Removed by |
|---|---|---|---|---|
| S3 | bucket `cleandocs-<account>-eu-west-1` (versioned; originals, processed JSON, kb-source chunks, `deploy/` packages) | `cleandocs-core` (**Retain**) | yes (storage) | teardown step 6 (stack keeps it on purpose) |
| DynamoDB | `cleandocs-registry`, `document-ingest-trail` | core (**Retain**) | small | teardown step 6 |
| S3 Vectors | vector bucket `cleandocs-vectors-<account>`, index `cleandocs-index` | core | yes (storage + queries) | core stack delete (index must be empty first) |
| Bedrock | Knowledge Base + data source (ids in stack outputs), role `cleandocs-kb-role` | core | idle $0; embedding on sync | core stack delete |
| SQS | `cleandocs-file-queue`, `cleandocs-file-dlq` | core | ~0 | core stack delete |
| IAM | users `cleandocs-ingest|processing|indexing` **and their access keys** | core + `create-access-keys.ps1` | no, but a security risk | keys: teardown step 3; users: core stack delete |
| IAM | roles `cleandocs-kb-role`, `cleandocs-dispatcher-role`, `cleandocs-workflow-role` | core + orchestration | no | stack deletes |
| Step Functions | `cleandocs-ingestion`, `cleandocs-file` | orchestration | per transition | orchestration stack delete |
| Lambda + EventBridge | `cleandocs-dispatcher`, 5-minute schedule rule, Connection | orchestration | tiny | orchestration stack delete |
| Secrets Manager | secret `events!connection/cleandocs-services/...` (holds the service secret) | EventBridge Connection | ~$0.40/month | removed with the Connection |
| CloudWatch Logs | `/aws/lambda/cleandocs-dispatcher`, `/aws/vendedlogs/states/cleandocs-*`, plus any service logs | orchestration | small | teardown step 7 |
| Textract / Bedrock models | nothing stored; pay per use | - | only when used | nothing to delete |

Not in AWS but linked to the account: Render env groups hold the 3 AWS access keys; the SERVICE_AUTH_SECRET exists in Render and in the Secrets Manager secret above.

## 2. Delete everything (one command)
```powershell
cd E:\dev\docindex
.\scripts\teardown-account.ps1 -Inventory     # look first
.\scripts\teardown-account.ps1 -Execute       # type your 12-digit account id to confirm
```
What it does, in this order (the order matters):
1. stops running Step Functions executions;
2. deletes all documents, registry/trail rows and Knowledge Base vectors (`clean_all.py`);
3. deletes the IAM access keys (a user that still has keys makes the stack deletion fail);
4. deletes stack `cleandocs-orchestration`;
5. deletes stack `cleandocs-core`;
6. empties and deletes the S3 bucket (all versions) and both DynamoDB tables;
7. deletes leftover log groups and prints the inventory again (should be empty).

The script has not been run end to end. If a step fails, read the error, fix it by hand in the console (CloudFormation -> stack -> Events tab shows
which resource blocked) and run the script again; every step is safe to repeat.

## 3. Check that no charges remain
- Run `-Inventory` again: nothing should be listed.
- AWS console -> Billing and Cost Management -> Cost Explorer: look again the next day, group by Service.
- Also look for things this project never created but you may have in the same account: `family-docs-vectors` (a separate S3 Vectors bucket
  you had before) is NOT touched by this script.

## 4. Render and GitHub (manual, nothing is automatic)
- Render dashboard: delete the 16 `cleandocs-*-service` web services, the Blueprint `cleandocs`, and the env groups `cleandocs-auth`, `cleandocs-aws-ingest`, `cleandocs-aws-processing`, `cleandocs-aws-indexing`, `cleandocs-config`.
- Render -> Account Settings -> API Keys: delete any key you created.
- Keep the GitHub repo: it contains all the code you need for the new account.
