# Phase 2 — AWS infrastructure (deployed 2026-11-20, eu-west-1, account <ACCOUNT_ID>)

Stack `cleandocs-core`, template `infrastructure/cleandocs-core.yaml` (CloudFormation; validated with `cfn-lint`).
Deviation from Phase 0 note: plain CloudFormation instead of SAM, because no Lambda or `Transform` is needed yet and CloudFormation supports S3 Vectors and Bedrock KB natively (checked with `describe-type`). SAM can be layered on in Phase 7 for the dispatcher Lambda.

## Resources

| Resource | Name / id | Idle cost | Notes |
|---|---|---|---|
| S3 bucket | `cleandocs-<ACCOUNT_ID>-eu-west-1` | storage only | Private, SSE-S3, versioned, TLS-only. Deletes under `raw/` denied to everyone except `<admin-user>`. Lifecycle: `tmp/` 1 day, multipart abort 7 days, noncurrent versions 30 days. **Retain** on stack delete. |
| SQS queue + DLQ | `cleandocs-file-queue`, `cleandocs-file-dlq` | $0 | Visibility 60 s, long polling 20 s, redrive after 5 receives, DLQ retention 14 days, SQS-managed SSE. |
| DynamoDB | `cleandocs-registry`, `document-ingest-trail` | $0 idle | On-demand, no GSIs, PK/SK strings. **Retain** on stack delete. |
| S3 Vectors | bucket `cleandocs-vectors-<ACCOUNT_ID>`, index `cleandocs-index` | storage only | float32, 1024 dim, cosine; `AMAZON_BEDROCK_TEXT` and `AMAZON_BEDROCK_METADATA` non-filterable (required for KB chunk text). Separate from the existing `family-docs-vectors`, which is untouched. |
| Bedrock Knowledge Base | `<KB_ID>` (ACTIVE), data source `<DS_ID>` | $0 idle | Titan Text Embeddings V2, data source = `kb-source/` prefix, chunking `NONE`, delete policy DELETE. Role `cleandocs-kb-role`. |
| IAM users | `cleandocs-ingest`, `cleandocs-processing`, `cleandocs-indexing` | $0 | Least-privilege tiers for Render. **No access keys exist yet**; create them only when deploying to Render. |

Not created yet (later phases): Step Functions state machines (Phase 7), dispatcher Lambda and its role (Phase 7), CloudWatch log groups with short retention (with the Lambda/state machines), AWS Budgets alert (needs your email address).

Charges that can occur while idle: S3 and S3 Vectors storage (cents), CloudWatch log storage once logging exists. Everything else is pay per use. No KMS keys, NAT gateways, load balancers or always-on compute were created.

## Commands (PowerShell)

```powershell
.\scripts\deploy-infra.ps1                 # create / update
python scripts\kb_smoke_test.py            # real end-to-end check of kb-source -> KB -> S3 Vectors
.\scripts\destroy-infra.ps1                # delete stack, keep bucket + tables
.\scripts\destroy-infra.ps1 -PurgeData     # also destroy all data (asks you to type DELETE)
```

## Verification status

Verified live with the AWS CLI after deploy: public access block, versioning, encryption, queue redrive, both tables ACTIVE and on-demand, vector index dimension/metric/non-filterable keys, KB ACTIVE with `S3_VECTORS`, data source AVAILABLE with chunking `NONE`.

**Smoke test passed (2026-11-20, run by Rajat):** one chunk plus `.metadata.json` sidecar in `kb-source/` was indexed (1 scanned, 1 new document indexed, 0 failed), the S3 Vectors index went from 0 to 1 vector, and after deleting the object a second sync reported 1 document deleted and the index returned to 0. This confirms: chunking `NONE` (one object = one chunk), sidecar metadata accepted, and deletion via re-sync removes vectors.
