# Phase 8: reconciliation

HTTP 200 and registry fields are claims. Reconciliation re-checks durable evidence and sets the final status.

## What is checked, per document
| Check | Evidence | Tier |
|---|---|---|
| original_exists | raw S3 object: exists, size and SHA-256 match the registry | required |
| artifact_* | validation (direct uploads only), normalized, cleaned, duplicate, extraction, metadata, chunks, embedding manifest: each exists, parses, belongs to the document | required |
| chunk_objects | every chunk `.txt` and `.metadata.json` object in the manifest exists | required |
| chunk_text_integrity | optional (`verify_chunk_text`): re-hash each chunk against the manifest | required |
| orphan_chunk_objects | no `kb-source/{doc}/` object outside the manifest | indexing |
| chunks_indexed | the Knowledge Base itself lists every chunk as INDEXED | indexing |
| stale_kb_entries | the KB holds no entry for the document that is not in the manifest | indexing |
| registry_stage / registry_indexing_state / chunk_count_consistent | registry agrees with the evidence | indexing |
| audit_* | the immutable trail has a latest terminal COMPLETED or SKIPPED row for every required service (archive entries: the parent's archive row) | audit |
| graph_status | corrected to what the trail shows: NOT_IMPLEMENTED, NOT_CONFIGURED or FAILED, never an unsupported COMPLETED | info |

Verdict: any failed `required` check -> FAILED; else any failed `indexing`/`audit` check -> PARTIAL; else COMPLETED.
DUPLICATE (canonical document must exist and not be failed), REJECTED and FAILED documents are verified but never upgraded.
Documents still running stay PROCESSING and the ingestion is not finalised.

## Ingestion level
Archive manifests: every extracted file must be tracked in the ingestion. KB job must be COMPLETE and have an audit row.
Status rule (shared with manifest-service): all COMPLETED/DUPLICATE -> COMPLETED; nothing usable -> FAILED; otherwise PARTIAL.
A PARTIAL document counts as usable, so it never makes an ingestion FAILED on its own.
After reconciliation (`reconciled_at` set) the manifest status poll no longer overwrites the ingestion status.
Report: `processed/ingestions/{id}/reconciliation.json`, also `GET /api/v1/ingestions/{id}/reconciliation`.

## Calls and limits
- `POST /api/v1/process` (workflow): time-budgeted (`max_seconds`, default 40, because HTTP Tasks stop at 60 s). Returns `complete: false` and the workflow calls again. Documents already checked for the current indexing job are skipped unless `force=true`.
- `POST /api/v1/documents/{id}/reconcile?repair=true&verify_chunk_text=true`: operator re-check of one document. `repair` deletes only orphan chunk objects.
- A new indexing job changes the run token, so evidence is re-checked after re-indexing.
- The workflow reconciles after indexing (and when nothing needed indexing). If the reconciliation service is down the workflow still rolls up; `reconciled_at` stays unset, which shows it did not run.

## Limits
- Deletion workflow is not built. Reconciliation detects the effects of out-of-band deletion (missing original, chunk objects) and orphans/stale KB entries, and removes orphan objects. Stale KB entries disappear at the next indexing job; until then the document stays PARTIAL.
- IAM: runs on the cleandocs-indexing tier (KB read, S3 read, delete in `kb-source/`, write `processed/ingestions/`, registry, trail).
- Cost: reads only (S3 HEAD/GET/LIST, DynamoDB queries, one KB listing per call). Not run against live AWS yet.
