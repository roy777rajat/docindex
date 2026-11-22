# Building CleanDocs from scratch in a NEW AWS account

[Back to the handbook index](README.md)

Time: about 1.5 hours (most of it Render builds). The repo has everything; only account-specific values change.
Assumptions: Windows + PowerShell, AWS CLI v2, `uv`, Python 3.12, Git, the GitHub repo `roy777rajat/docindex`, a Render account (Free plan).

## 0. Values you must have (and where to find them)
| Value | Where to find it | Used for |
|---|---|---|
| New AWS account id (12 digits) | AWS console, top-right menu; or `aws sts get-caller-identity` | everything (bucket name, Render config) |
| An admin IAM user + access key for the CLI (not the root user) | IAM -> Users -> Create user -> attach `AdministratorAccess` -> Security credentials -> Create access key | running the scripts. Then `aws configure` (region `eu-west-1`) |
| Region | keep `eu-west-1` (Titan, Nova Micro EU profile, S3 Vectors, Textract all available there) | all |
| Bedrock model access | Bedrock console -> Model access (or "Models") -> make sure **Titan Text Embeddings V2** and **Amazon Nova Micro** are available. Test command in step 2 | embeddings, enrichment |
| `SERVICE_AUTH_SECRET` (you create it, 32+ chars) | generate in step 3; **save it in a password manager** (it cannot be read back from AWS) | Render group `cleandocs-auth`, orchestration stack |
| Knowledge Base id and data source id | printed by step 4 (`.\scripts\show-config.ps1`); Bedrock console -> Knowledge bases | Render config (`render.yaml`) |
| 3 AWS access keys for Render | created by `create-access-keys.ps1` in step 5, shown once in `secrets\*.env` | Render env groups |
| Render API key | Render -> Account Settings -> API Keys -> Create | `render-urls.ps1`, `deploy-render.ps1` |
| Render service URLs | `render-urls.json` (script) or each service page in Render | workflows, your curl commands |
| Step Functions ARNs | `.\scripts\show-config.ps1` or CloudFormation -> `cleandocs-orchestration` -> Outputs | check only (the Blueprint builds the ingestion ARN from the account id) |

## 1. Get the code and tools
```powershell
git clone https://github.com/roy777rajat/docindex.git E:\dev\docindex ; cd E:\dev\docindex
aws configure                      # new admin user's keys, region eu-west-1
aws sts get-caller-identity        # must show the NEW account id
```

## 2. Check Bedrock model access (before spending time on anything else)
```powershell
'{"modelId":"eu.amazon.nova-micro-v1:0","messages":[{"role":"user","content":[{"text":"Say OK"}]}]}' | Out-File q.json -Encoding ascii
aws bedrock-runtime converse --region eu-west-1 --cli-input-json file://q.json --query "output.message.content[0].text"
Remove-Item q.json
```
`"OK"` = ready. An AccessDenied/"model access" error: enable the model in the Bedrock console (Model access) and retry.

## 3. Create the service secret
```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$env:SERVICE_AUTH_SECRET = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
$env:SERVICE_AUTH_SECRET          # SAVE THIS VALUE NOW in your password manager
```
Keep this PowerShell window open for step 6 (the variable lives only in this window).

## 4. Create the AWS infrastructure
```powershell
.\scripts\deploy-infra.ps1          # stack cleandocs-core: S3, SQS, DynamoDB, S3 Vectors, Knowledge Base, IAM users (~5-10 min)
.\scripts\show-config.ps1           # note KnowledgeBaseId and DataSourceId
```
`deploy-infra.ps1` makes the logged-in IAM user the only one who may delete under `raw/`. Use an IAM **user** (not an SSO/assumed role) as the admin.

## 5. Create the Render AWS keys
```powershell
.\scripts\create-access-keys.ps1    # writes secrets\aws-ingest.env, aws-processing.env, aws-indexing.env
```

## 6. Point the repo at the new account and push
```powershell
python scripts\build_render_yaml.py --account <NEW_ACCOUNT_ID> --kb-id <KnowledgeBaseId> --ds-id <DataSourceId>
git add -A ; git commit -m "Point Render blueprint at the new AWS account" ; git push
```
(Search the repo for the old account id `141571819444` in docs only if you want the text updated; the code takes the account from AWS.)

## 7. Render
1. Dashboard -> Environment Groups -> create **before** the Blueprint (names must match exactly):
   `cleandocs-auth` (variable `SERVICE_AUTH_SECRET` = your saved secret), and `cleandocs-aws-ingest`, `cleandocs-aws-processing`, `cleandocs-aws-indexing` ("Add from .env", paste the matching file from `secrets\`).
2. `Remove-Item -Recurse -Force secrets`
3. New -> Blueprint -> repo `docindex`, branch `main`, file `render.yaml`, Blueprint name `cleandocs` -> Apply. 16 services build (several minutes each).
4. Create a Render API key, then:
```powershell
$env:RENDER_API_KEY = "rnd_..."
.\scripts\render-urls.ps1           # writes render-urls.json (real URLs)
```

## 8. Orchestration (workflows)
```powershell
.\scripts\deploy-orchestration.ps1 -UrlsFile render-urls.json     # uses $env:SERVICE_AUTH_SECRET from step 3
```

## 9. Test
```powershell
python scripts\smoke_test_pipeline.py --urls-file render-urls.json
```
Then upload a real file as in the operator handbook. Delete the Render API key when finished.

## 10. Your documents
Nothing is copied from the old account. Upload your original files again (`owner` = your user name). If you only have them in the old
bucket, download `raw/documents/*/original/*` before running the teardown.

## Troubleshooting new-account problems
| Symptom | Cause / fix |
|---|---|
| `AccessDenied` on Bedrock | model not enabled for the account, or the IAM `processing` user stack was not updated; rerun step 4 |
| Stack rollback in `cleandocs-core` | open CloudFormation -> Events, read the first FAILED reason; often a name already exists (a leftover from an old attempt) |
| Render: "non-existent group" | create the env groups first (step 7.1) |
| Service `/health/ready` not ready | wrong AWS keys in the group, or the secret/groups missing; check the service Environment tab |
| 401 Unauthorized on any call | secret differs between Render `cleandocs-auth` and the orchestration stack; redo step 8 with the same value |
