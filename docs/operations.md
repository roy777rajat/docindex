# Operations runbook

All service calls need `Authorization: Bearer <SERVICE_AUTH_SECRET>`. PowerShell helper used below:
```powershell
$h = @{ Authorization = "Bearer $env:SERVICE_AUTH_SECRET" }
$u = Get-Content render-urls.json | ConvertFrom-Json      # $u.manifest, $u.reconciliation, $u.'vector-indexing' ...
```

## Where is my document?
- Ingestion status and per-file counts: `Invoke-RestMethod -Headers $h "$($u.manifest)/api/v1/ingestions/<ingestion_id>"`
- Final evidence-based report: `Invoke-RestMethod -Headers $h "$($u.reconciliation)/api/v1/ingestions/<ingestion_id>/reconciliation"`
- One document: `.../api/v1/documents/<document_id>/reconciliation`
- Every service call is in the DynamoDB table `document-ingest-trail` (partition `DOC#<document_id>`), and each workflow run is under Step Functions.
- Citation data for retrieval later: `docs/citations.md`.

## Re-check one document (and clean orphans)
```powershell
Invoke-RestMethod -Method Post -Headers $h "$($u.reconciliation)/api/v1/documents/<id>/reconcile?repair=true&verify_chunk_text=true"
```

## A document failed: replay
1. Read the failure: the reconciliation report, the trail, or the DLQ record (`failed_stage`). Fix the cause first.
2. `python scripts\replay_dlq.py` (dry run), then `python scripts\replay_dlq.py --apply`.
3. Replay re-queues the file (attempt + 1); finished stages are skipped. Wait for its file workflow to finish (Step Functions).

### After a replay
The ingestion workflow has already ended, so publish and re-check by hand:
```powershell
$ing = "<ingestion_id>"
$body = @{ context = @{ ingestion_id = $ing; trace_id = "replay" } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json -Body $body "$($u.'vector-indexing')/api/v1/process"
# repeat until state is COMPLETE (about a minute apart):
Invoke-RestMethod -Method Post -Headers $h "$($u.'vector-indexing')/api/v1/ingestions/$ing/index/poll"
$body = @{ context = @{ ingestion_id = $ing; trace_id = "replay" }; parameters = @{ force = $true } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $h -ContentType application/json -Body $body "$($u.reconciliation)/api/v1/process"
```

## Routine
- First request after idle takes about a minute per sleeping service; the workflows already wait for readiness.
- Rotate the service secret: new value in Render group `cleandocs-auth`, then `.\scripts\deploy-orchestration.ps1` with the same value; redeploy services.
- Rotate AWS keys: `aws iam list-access-keys --user-name cleandocs-processing`, create a new key (`create-access-keys.ps1` after removing its `secrets\aws-*.env`), update the Render group, delete the old key with `aws iam delete-access-key`.
- Textract page budget: `TEXTRACT_MONTHLY_PAGE_CAP` (default 300) on extraction-service.
- Stuck gate (rare): the dispatcher frees a gate whose execution ended or whose lease expired. Nothing to do; it retries every 5 minutes.

## Tear down
`.\scripts\destroy-infra.ps1` (AWS core; the bucket is purged by `scripts\purge_bucket.py`), delete the `cleandocs-orchestration` stack, delete the Render services and env groups, and delete the IAM access keys.
