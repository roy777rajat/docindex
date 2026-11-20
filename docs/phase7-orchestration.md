# Phase 7: orchestration

## Flow
upload -> SQS -> **dispatcher Lambda** -> **gate** (one document at a time) -> **file workflow** (Step Functions) -> next message.
An ingestion has its own **ingestion workflow**: validation -> (archive) -> manifest -> publish one SQS message per file -> wait until every file is settled -> start vector indexing -> poll -> roll-up.

## Gate (single-flight)
DynamoDB item `GATE#GLOBAL/LOCK` in `cleandocs-registry`: `holder_document_id`, `workflow_arn`, `lease_expires_at`, `fence`.
Acquire/release/extend are conditional writes. The dispatcher reaps a holder whose execution finished (`DescribeExecution`) or whose lease expired.
Lambda reserved concurrency is not used: this account's Lambda concurrency limit is 10, so a reservation is not allowed. The gate gives the single-flight guarantee instead.

## Dispatcher
Triggered every 5 minutes (fallback) and by async nudges from the workflows. It receives one message, acquires the gate, starts execution `{doc}-a{attempt}`,
records the ARN, then deletes the message. If starting fails it releases the gate and the message returns to the queue. After maxReceiveCount the message goes to the DLQ.

## Render free tier
- Step Functions HTTP Tasks have a hard 60 s cap. Every call is preceded by a readiness GET with retry (15 s, 1.5x, 8 attempts, about 12 min) so a sleeping service can wake (about a minute per service).
- OCR is bounded: extraction processes `ocr_pages_per_call` pages (default 4) per call, persists progress in `extraction/ocr_progress.json`, and the workflow loops until `complete` is true.
- Retried: timeouts and HTTP 429/502/503/504. Fail fast: 404/409/415/422. 500 is not retried.

## Failure handling
Business failure: document marked failed by the service, gate released, no replay record.
Other failure: `MarkDocFailed` -> replay record on the DLQ -> gate released -> dispatcher nudged -> execution fails.
Optional stages (entity-extraction, graph) never fail the document.

## IAM tiers
upload-service and archive-service: cleandocs-ingest (archive writes `raw/`). Normalization..embedding: cleandocs-processing. vector-indexing: cleandocs-indexing.

## Deploy (you run this; I could not deploy it from here)
```powershell
$env:SERVICE_AUTH_SECRET = "<same value as on Render>"
.\scripts\deploy-orchestration.ps1 -UrlTemplate "https://cleandocs-{service}-service.onrender.com"
```
Then set `STEP_FUNCTIONS_INGESTION_ARN` on upload-service to the stack output `IngestionStateMachineArn`.

## Not yet
Reconciliation step in the ingestion workflow (Phase 8). Nothing here has run against live Render or AWS.
