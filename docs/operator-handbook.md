# CleanDocs AI - Operator Handbook

Upload, track, reconcile, delete (Windows PowerShell). [Back to the handbook index](README.md)


## 1. One-time setup in every PowerShell window

Run from the repo folder `E:\dev\docindex`. Put your real secret in the first line (it is in your own saved file; never paste it into chat, Git or screenshots).

```powershell
$H  = "Authorization: Bearer <YOUR SERVICE_AUTH_SECRET>"
$U  = "https://cleandocs-upload-service.onrender.com"
$M  = "https://cleandocs-manifest-service.onrender.com"
$R  = "https://cleandocs-reconciliation-service.onrender.com"
```

If Render gave a service a different URL, the real URLs are in `render-urls.json` (create it with `.\scripts\render-urls.ps1`).
All calls except `/health/*` need the `Authorization` header. There are no Swagger pages.

## 2. Render sleeps - wake it first

**What happens.** Render Free services go to sleep after about 15 minutes without traffic and need roughly one minute to wake.
Your first upload after a quiet period can therefore take 1-2 minutes or fail with a timeout or a 502/503 error.

**What is already protected.** The Step Functions workflows check `/health/ready` before every stage and retry for about 12 minutes,
so a sleeping stage service does not break an ingestion; it only makes it slower (each service wakes when its stage starts).
The part you control - the upload call and your status polling - is not protected, hence the warm-up.

**Always do this before an upload:**

```powershell
.\scripts\warm-up.ps1                    # wakes all 16 services in parallel, prints UP or NOT UP for each (~1-2 min)
.\scripts\warm-up.ps1 -Only upload,manifest     # or only what you need right now
```

**If an upload call times out or returns 502/503:** run the same command again with the **same** `Idempotency-Key`. It is safe: if the
first attempt actually succeeded you get the stored answer back (`"idempotent_replay": true`) instead of a second copy.
Use a new key only when you really want a new upload.

Tip: wake-up uses Render's free instance hours (shared across your free services; see Render -> Billing/Usage). Warm up when you are about to work, not all day.

## 3. Upload a file

Use a **new** `Idempotency-Key` per new upload (8-128 characters: letters, digits, `.` `_` `-`). `owner` is optional (default `ADMIN`);
use your user name, e.g. `rajat`. Allowed: PDF, JPG/PNG/TIFF, DOCX, XLSX, CSV, TXT, JSON, XML and ZIP of those (max 25 MB; a ZIP up to 50 files).

```powershell
# One PDF
curl.exe -sS --max-time 180 --retry 3 --retry-all-errors -X POST "$U/api/v1/uploads" -H $H `
  -H "Idempotency-Key: pdf-2026-11-22-001" -F "owner=rajat" -F "file=@C:\docs\report.pdf"

# A ZIP (every file inside is processed, each inherits the owner)
curl.exe -sS --max-time 180 --retry 3 --retry-all-errors -X POST "$U/api/v1/uploads" -H $H `
  -H "Idempotency-Key: zip-2026-11-22-001" -F "owner=rajat" -F "file=@C:\docs\TEST-ZIP3.zip"
```

The answer contains the numbers you need. Save them:

| Field | Meaning |
|---|---|
| `ingestion_id` (`ing_...`) | one upload = one ingestion; use it to track and reconcile |
| `document_id` (`doc_...`) | the file you uploaded (for a ZIP: the ZIP itself) |
| `workflow_started: true` | the Step Functions workflow is running |
| `owner` | who owns it |
| `idempotent_replay` | `true` = answer replayed from an earlier identical request |

```powershell
$ING = "ing_..."          # paste yours
```

## 4. Track progress

**4.1 Status roll-up (poll about once a minute):**

```powershell
curl.exe -sS "$M/api/v1/ingestions/$ING" -H $H
```

| Field | Meaning |
|---|---|
| `status` | `PROCESSING` -> `COMPLETED` (all good), `PARTIAL` (usable but something needs a look), `FAILED` |
| `total_files`, `counts` | files found (ZIP children counted) and their statuses |
| `pending_files` | still being processed; `all_settled: true` = nothing left running |
| `archives` | the ZIP document id(s) |

Typical time: a few minutes per file (files are processed one at a time; scanned pages and Render wake-ups add time).

**4.2 See each step (AWS console):** Step Functions -> State machines -> `cleandocs-ingestion` -> Executions -> the one named with your
`ingestion_id`; every file also has an execution in `cleandocs-file`. Green = done, red = failed (click the red state, "Exception").
The `Enrichment` step may be skipped or fail without failing the document - that is by design.

**4.3 Every call that happened (audit):** DynamoDB -> table `document-ingest-trail` -> items with partition key `DOCUMENT#<document_id>`.
Each service writes one STARTED and one COMPLETED row.

**4.4 Failed files:** the SQS dead-letter queue `cleandocs-file-dlq` holds a record for each failed file. Fix the cause, then replay
(see `docs/operations.md`: `python scripts\replay_dlq.py`, dry run first, then `--apply`).

## 5. Reconcile (prove it is really done)

Statuses are claims until reconciliation re-checks the stored evidence (S3 objects, Knowledge Base entries, audit rows). The workflow runs it
automatically at the end. To read the report:

```powershell
curl.exe -sS "$R/api/v1/ingestions/$ING/reconciliation" -H $H
```

Check the following:

| Check | Meaning if it fails |
|---|---|
| `original_exists`, `artifact_*`, `chunk_objects` (tier `required`) | a file or stage output is missing - the document is FAILED; re-upload or replay |
| `sidecar_owner_matches_registry` (`required`) | chunk metadata has a different owner than the registry (old chunks from before the owner field): delete and re-upload |
| `chunks_indexed`, `orphan_chunk_objects`, `stale_kb_entries`, `sidecar_attributes_present` (`indexing`) | not fully searchable yet - wait for the indexing job or re-run reconcile |
| `audit_*` (`audit`) | a service call is missing from the audit trail |

Verdict: any failed `required` check = `FAILED`; else any failed `indexing`/`audit` check = `PARTIAL`; else `COMPLETED`.
Re-check one document and remove orphan chunk objects:

```powershell
curl.exe -sS -X POST "$R/api/v1/documents/<doc_id>/reconcile?repair=true&verify_chunk_text=true" -H $H
```

## 6. Look at what was stored

* **Search test:** Bedrock console -> Knowledge bases -> your KB -> **Test** -> "Standard retrieval only" -> type a question. Open the result
  details to see the chunk metadata (`owner`, `doc_type`, `tags`, `has_pii`, `filename`, `page_start` ...).
* **OCR text of a scanned page:** `aws s3 cp s3://<bucket>/processed/documents/<doc_id>/extraction/extraction.json ex.json` (delete `ex.json` after; it contains document text and must never be committed).
* **LLM labels:** `processed/documents/<doc_id>/metadata/enrichment.json` (document type, tags, description, PII flag).
* Bucket name and ids: `.\scripts\show-config.ps1`.

## 7. Delete documents (any time)

Short version: `.\scripts\cleandocs.ps1 list`, then `.\scripts\cleandocs.ps1 delete <doc_or_ing_id>`.
Full guide with all options and what gets removed: [Delete documents](delete-documents.md).

## 8. When something looks wrong

| Symptom | Likely cause and fix |
|---|---|
| `curl: (3) URL rejected` | `$U` / `$H` not set in this window (section 1) |
| Timeout, 502, 503 on upload | service asleep: run `warm-up.ps1`, repeat with the same `Idempotency-Key` |
| `401 Unauthorized` | wrong secret in `$H` (must equal the Render `cleandocs-auth` value) |
| `BAD_IDEMPOTENCY_KEY` | key must be 8-128 chars `A-Za-z0-9._-` |
| `BAD_OWNER` | owner may contain only `A-Za-z0-9._@-`, max 64 |
| Upload answer with `"idempotent_replay": true` | you reused a key: that is the first upload's answer, no new upload happened |
| Status stays `PROCESSING` > 30 min | open Step Functions execution (4.2); a red state names the failing stage; check the DLQ (4.4) |
| `DUPLICATE` | identical bytes were uploaded before; delete the earlier one if you want to re-ingest |
| `OCR_DISABLED` / `TEXTRACT_BUDGET_EXCEEDED` | scanned file but OCR is off or the monthly page cap (default 300) is used up (`TEXTRACT_MONTHLY_PAGE_CAP` in Render) |
| `reconciliation` says `PARTIAL` | indexing still running or a stale entry; wait 2 minutes and run reconcile again |
| Nothing found by search | chunk not `INDEXED` yet (check reconciliation) or text unreadable (look at the OCR text, section 6) |

## 9. Keeping secrets safe

* Never put the service secret, AWS keys or a Render API key into Git, a screenshot or a chat. Keep them in a password manager.
* Close PowerShell windows that contain `$H` when finished. Delete any Render API key you no longer need.
* Files like `ex.json`, `m.json`, `q.json` created during checks can contain personal text: delete them after use (they are in `.gitignore`).
* Your documents contain personal data (IDs, certificates): the S3 bucket is private; do not share chunk output publicly.
