# Phase 10 plan: owner-aware, LLM-enriched metadata (DRAFT, decisions below recorded 2026-11-22, nothing is built yet)

Goal: make the index usable by a later query-api (RAG chatbot): per-user access control, richer filterable metadata,
LLM-derived doc type / tags / description, a queryable catalog, and deletion. The query-api itself is a separate project.

## 0. Feasibility (checked against the current code)
| Item | Verdict | Basis |
|---|---|---|
| `owner` at upload, default `ADMIN` | Easy | upload already takes a `label` form field; add `owner` the same way, store on ingestion + document rows, copy to ZIP children in archive-service |
| `owner` in every chunk sidecar, filterable | Easy | embedding-service already builds the sidecar from registry + metadata; one more attribute |
| LLM doc_type / tags / description / language / date | Possible | `classifier_model_id` already exists in config (unused); the `processing` IAM user already has `bedrock:InvokeModel` on `foundation-model/*` in the region. To verify: which model is enabled in eu-west-1, and whether it needs an EU inference-profile ARN (then the policy needs that ARN too) |
| New enrichment service, soft-fail | Possible | same pattern as entity-extraction (never fails the document); needs one new "soft stage" in `workflows/asl.py` |
| Registry catalog queries | Needs change | registry has only PK/SK, no GSI; add GSIs (CloudFormation allows one GSI change per stack update, so two updates) |
| Deletion | New workflow | nothing exists; IAM needs delete on `processed/*` and `raw/*` (indexing user only has delete on `kb-source/*`) |

Hard limits to respect (verify against current AWS docs before building, I have not confirmed the numbers):
- S3 Vectors caps filterable metadata per vector (about 2 KB). Our sidecar budget is 1,500 bytes, so every new attribute spends it.
- The index's non-filterable keys are fixed when the index is created (today only `AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`).
  Anything else in the sidecar is filterable. Changing this means recreating the index and re-indexing everything.
- Bedrock filter operators supported for the S3 Vectors store (equals, in, range, listContains ...) should be confirmed.

## 1. Security model (important)
`owner` is a label, not authentication. The services only know a shared Bearer secret. Real enforcement belongs in query-api:
- It authenticates the end user and derives `role` and `owner` itself (never from request input).
- `ADMIN` -> no owner filter. Any other user -> mandatory filter `owner == <user>` on every retrieval. Fail closed if the user is unknown.
- Default owner `ADMIN` means a forgotten owner makes a document private to admins (safe default).
- The LLM never decides owner, access or visibility.
- Not in scope now, design allows it later: sharing (`shared_with` as a string list + `listContains`).

## 2. Sub-phases (approval between each, same as before)

### 10A. Owner + richer filterable attributes
- upload-service: form field `owner` (default `ADMIN`; 1-64 chars `A-Za-z0-9._@-`; `ADMIN` reserved, stored upper case; other values lower-cased). Echo it in the response.
- registry: `owner` on ingestion and document rows; archive-service copies the parent's owner to every child. Idempotent replay returns the same owner.
- embedding-service sidecar adds: `owner`, `file_kind`, `language`, `uploaded_at` (epoch seconds, number), `doc_date` (epoch, number, when found), `ocr_confidence` (number, when OCR was used), `doc_type`, `tags` (string list), `has_pii` (boolean).
  Kept OUT of the sidecar to save budget: title and description (resolved by `document_id` at query time).
- reconciliation: new checks `sidecar_owner_matches_registry` (tier required) and `sidecar_attributes_present`.
- Existing test documents have no owner in their sidecars. Plan: delete and re-ingest them (they are test data), not backfill.
- Tests: upload/default/invalid owner, ZIP children inherit, sidecar size budget with all attributes at max length, reconciliation mismatch.

### 10B. enrichment-service (LLM) = "enrichMetadataAndLLMReasoning"
- New service #16 (`/api/v1/process`, same contract, `processing` tier, Render service `cleandocs-enrichment-service`).
- Position: after `metadata`, before `chunking`. Soft stage: on any LLM or validation failure, the document continues with the rule-based metadata and `enrichment_status = SKIPPED|FAILED` is recorded. It never fails a document.
- One LLM call per document (not per chunk): input is the cleaned text, truncated (first N characters plus headings), as data, never as instructions.
- Output (strict JSON schema, validated, temperature 0):
  `doc_type` from a controlled vocabulary, `doc_type_confidence`, `tags` (max 6, each max 24 chars, lower-case), `description` (max 300 chars), `title`, `language`, `doc_date`, `contains_pii` + `pii_types` (e.g. PAN, AADHAAR, BANK_ACCOUNT).
- Stored at `processed/documents/{id}/metadata/enrichment.json` with model id, prompt version, input checksum. Same input + model + prompt version = SKIP.
- Merge rule: LLM `doc_type` wins only if confidence >= threshold (default 0.6), else keep the rule-based one. Registry gets `doc_type`, `tags`, `description`, `title`, `language`, `contains_pii`.
- Safeguards: prompt-injection tolerant (text in a delimited block, output schema only), no free-form output stored unvalidated, per-document token cap, monthly document cap (like the Textract cap), PII flag is informational, not a gate.
- Cost and latency: one short call per document, a few cents per hundred documents at small-model prices (estimate; confirm after choosing the model). Must finish inside the 60 s HTTP-task limit, so use a timeout and truncated input.
- Wiring: `workflows/asl.py` (soft stage), `workflows/build.py`, `scripts/build_render_yaml.py`, `scripts/local_gateway.py`, local test helper, Render Blueprint sync + manual deploy, docs.
- Tests: schema validation, vocabulary enforcement, soft-fail paths, idempotent SKIP, prompt-injection sample, ASL order test, Blueprint test (16 services).

### 10C. Registry catalog indexes
- GSI1 `owner-index`: partition `GSI1PK = OWNER#<owner>`, sort `GSI1SK = <uploaded_at>#<document_id>` (documents only). Query: "all documents of USER-A, newest first", ADMIN lists via a scan-safe admin path or a second GSI by status/date.
- Optional GSI2 for `doc_type`. Added in separate stack updates (CloudFormation limit).
- Small read API (catalog list/get by owner, type, date) inside an existing service or in query-api: decide in review.

### 10D. Deletion workflow
- `DELETE`-style request (document or ingestion), parameters: `requested_by`, `delete_original` (default true).
- Steps: mark `DELETING` -> remove `kb-source/{doc}/*` -> start KB ingestion job and poll -> verify the KB no longer lists the chunks -> delete `processed/documents/{doc}/*` (and the original) -> registry tombstone `DELETED` (keeps audit rows) -> reconciliation evidence.
- IAM: a new tier user `cleandocs-deletion` (delete on `raw/*`, `processed/*`, `kb-source/*`, Bedrock sync) + one more Render env group. Alternative: widen the indexing user (not recommended).
- Authorization (owner or ADMIN) is enforced by query-api; the service records `requested_by` in the audit trail.
- **Full clean** (requested): `POST /api/v1/purge` on the deletion service, ADMIN-only by the caller's contract.
  - Scope: everything this project stores: S3 `raw/`, `processed/`, `kb-source/` (never the bucket itself, the KB, the vector index or any stack resource), DynamoDB registry and
    optionally the audit trail, the file queue and DLQ, and the global gate row. Then a Knowledge Base sync removes the vectors, and the service verifies the KB lists nothing.
  - Guardrails: `dry_run` is the default and returns counts only; a real run needs `confirm = "DELETE-ALL-<account-id>"`; it refuses while any ingestion is in progress or the
    gate is held unless `force=true`; it records a purge summary (who, when, counts) in a small `purge/` report before deleting the trail; it deletes only keys under this project's prefixes.
  - Break-glass alternative that already exists: `scripts/purge_bucket.py` with your own admin credentials (no Render involvement). Both will be kept.
  - Per-owner purge (`owner = USER-A`) uses the 10C index.

## 3. Order, size and risk
Order: 10A -> 10B -> 10C -> 10D. Rough size: 10A small, 10B medium, 10C small, 10D medium.
Risks: sidecar size budget; index immutability for non-filterable keys (avoid by keeping long text out of the sidecar); LLM model access / inference profile in eu-west-1; Free Render cold starts for a 16th service; LLM mistakes in `doc_type` (mitigated by threshold, controlled vocabulary, and keeping the rule-based fallback).
Rollback: every change is additive. Enrichment can be bypassed by removing the soft stage; extra sidecar attributes are ignored by readers that don't use them.

## 4. Decisions (status)
| # | Decision | Status |
|---|---|---|
| 1 | Owner format as proposed; single owner now (sharing later) | Approved |
| 2 | Model: Amazon Nova Micro through the EU inference profile `eu.amazon.nova-micro-v1:0` (text only, cheapest). Checked on 2026-11-22 in eu-west-1: account is AUTHORIZED for Nova Micro, Nova Lite and Claude Haiku 4.5; a test call to Nova Micro with admin credentials returned in about 0.3 s. Config: `CLASSIFIER_MODEL_ID`. Fallback options: `eu.amazon.nova-lite-v1:0` (a little better, still cheap). Monthly document cap default 1,000. | Proposed, awaiting OK |
| 3 | Doc type vocabulary as listed | Approved |
| 4 | Description stays out of chunk text (see advice) | Proposed, awaiting OK |
| 5 | Delete and re-ingest test data; plus a full-clean capability (10D) | Proposed, awaiting OK |
| 6 | Separate `deletion` tier (IAM user + Render env group + service) | Proposed, awaiting OK |
| 7 | `contains_pii` / `pii_types` wanted | Approved |

### IAM change needed for the model (found while checking)
The `processing` user can invoke `foundation-model/*` only in the stack's own region. An EU inference profile may route a call to other EU regions,
so the policy must also allow: `bedrock:InvokeModel` on the profile ARN `arn:aws:bedrock:eu-west-1:<account>:inference-profile/eu.amazon.nova-micro-v1:0`
and on `arn:aws:bedrock:*::foundation-model/amazon.nova-micro-v1:0` (all regions, this one model only). To be verified with a real call using the
Render `processing` key before 10B is accepted. Approximate cost (from memory, confirm on the Bedrock pricing page): a 2,000-token document is about
a hundredth of a cent with Nova Micro.
1. Owner format and case rules as proposed above? Single owner only, or `shared_with` now?
2. Which Bedrock model for enrichment (check what is enabled in eu-west-1), and the monthly document cap.
3. The `doc_type` vocabulary (proposal: identity_document, bank_document, tax_document, insurance, employment, invoice, receipt, contract, report, correspondence, form, other).
4. Keep `description` out of chunk text (recommended) or prepend it to each chunk for better retrieval (costs vector quality tuning and re-indexing)?
5. Delete and re-ingest the current test data (recommended) or backfill owner = ADMIN?
6. Deletion: new `deletion` IAM tier (recommended) or widen the indexing tier? Delete originals by default?
7. Is `contains_pii` / `pii_types` wanted?


## Decisions recorded (2026-11-22)

- Model: Amazon Nova Micro (`eu.amazon.nova-micro-v1:0`) - approved.
- Description is NOT added to chunk text - approved.
- Delete one document and re-ingest, delete all and full re-ingest - approved. Deletion must also release the checksum claim and
  IDEMP rows so a re-ingested file is not flagged DUPLICATE.
- No separate `cleandocs-deletion` IAM tier. The deletion/purge service will run on the widened `indexing` tier
  (delete on `raw/*` and `processed/*`, DynamoDB delete, SQS purge) - to be confirmed when 10D starts.
- 10A done: `owner` form field (default ADMIN), ZIP children inherit it, sidecar gains owner/file_kind/language/uploaded_at,
  reconciliation checks `sidecar_owner_matches_registry` and `sidecar_attributes_present`.
- 10B done: enrichment-service (#16) is a soft stage between `metadata` and `chunking` (Nova Micro `eu.amazon.nova-micro-v1:0`,
  strict validated JSON, rules win when confidence >= 0.6, monthly cap `ENRICHMENT_MONTHLY_DOC_CAP` = 1000, output
  `processed/documents/{id}/metadata/enrichment.json`). Chunk metadata gains `tags`, `doc_date`, `has_pii`. The processing IAM
  tier gains the inference-profile + foundation-model ARNs (needs `deploy-infra`). Extraction/chunking left unchanged by decision.
