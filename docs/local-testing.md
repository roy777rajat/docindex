# Run all services on ONE local port and test them one by one

One FastAPI process on `http://127.0.0.1:8001` hosts all 16 services. Each service lives under its own path prefix:
`/upload/...`, `/validation/...`, `/normalization/...` and so on. It uses your REAL AWS account (your default credentials,
region eu-west-1). Step Functions and Render are not involved.

## Start / stop (PowerShell, repo root)
```powershell
# uv (recommended; no venv needed)
.\scripts\run_local.ps1                 # starts in background, prints  UP  http://127.0.0.1:8001/
.\scripts\run_local.ps1 -Foreground     # or run in the terminal with live logs, Ctrl+C to stop
.\scripts\stop_local.ps1                # stops the background server
```
Raw command if you prefer: `uv run --python 3.12 --with-requirements requirements-local.txt --with pydantic-settings python scripts\local_gateway.py`
Log of the background server: `logs\local\gateway.err.log`. Needs `aws sts get-caller-identity` to work.

Auth: every call except `/health/*` needs `Authorization: Bearer local-dev-secret-change-me-0123456789`.

## Service URLs (base `http://127.0.0.1:8001`)
There is no Swagger/OpenAPI page anywhere (disabled on purpose). Use curl, Postman or `scripts\local_step.py`.

| # | Service | Path prefix | Main call |
|---|---|---|---|
| 1 | upload | `/upload` | `POST /upload/api/v1/uploads` (multipart `file`, optional `owner`, header `Idempotency-Key`, 8-128 chars) |
| 2 | validation | `/validation` | `POST /validation/api/v1/process` |
| 3 | archive (zip only) | `/archive` | `POST /archive/api/v1/process` |
| 4 | manifest | `/manifest` | `POST /manifest/api/v1/process`, `GET /manifest/api/v1/ingestions/{ingestion_id}` |
| 5 | normalization | `/normalization` | `POST .../api/v1/process` |
| 6 | cleaning | `/cleaning` | same |
| 7 | duplicate | `/duplicate` | same |
| 8 | extraction (OCR here) | `/extraction` | same (repeat while `complete` is false) |
| 9 | metadata | `/metadata` | same |
| 10 | enrichment (LLM labels, optional) | `/enrichment` | same (calls Amazon Nova Micro; needs Bedrock access in your account) |
| 11 | chunking | `/chunking` | same |
| 12 | embedding | `/embedding` | same |
| 13 | vector-indexing | `/vector-indexing` | `POST .../api/v1/process`, then `POST .../api/v1/ingestions/{id}/index/poll` |
| 14 | reconciliation | `/reconciliation` | `POST .../api/v1/process`, `GET .../api/v1/ingestions/{id}/reconciliation` |
| 15 | entity-extraction (optional) | `/entity-extraction` | same |
| 16 | graph (skeleton) | `/graph` | same, returns NOT_IMPLEMENTED |

`http://127.0.0.1:8001/` lists them. Every service has `/health/live` and `/health/ready` (no auth). The audit trail is in the DynamoDB table `document-ingest-trail`.

## Walk one PDF through (curl; use `curl.exe` in PowerShell)
PowerShell removes the inner quotes of JSON passed on the command line, so bodies go through small files. Paste once:
```powershell
$H = "Authorization: Bearer local-dev-secret-change-me-0123456789"
$B = "http://127.0.0.1:8001"
function Body($ing,$doc,$tr) { $f = New-TemporaryFile; "{`"context`":{`"ingestion_id`":`"$ing`",`"document_id`":`"$doc`",`"trace_id`":`"$tr`",`"workflow_execution_id`":`"local`"},`"parameters`":{}}" | Set-Content $f -Encoding ascii; $f.FullName }
function IBody($ing,$tr) { $f = New-TemporaryFile; "{`"context`":{`"ingestion_id`":`"$ing`",`"trace_id`":`"$tr`",`"workflow_execution_id`":`"local`"},`"parameters`":{}}" | Set-Content $f -Encoding ascii; $f.FullName }
function Call($svc,$file) { curl.exe -sS -X POST "$B/$svc/api/v1/process" -H $H -H "Content-Type: application/json" --data-binary "@$file" }
```
**1. Upload** (the key must be 8-128 chars; reusing a key replays the first answer; `owner` is optional, default `ADMIN`):
```powershell
curl.exe -sS -X POST "$B/upload/api/v1/uploads" -H $H -H "Idempotency-Key: pdf-test-001" -F "owner=ADMIN" -F "file=@C:\docs\report.pdf"
```
Put the three ids from the answer in variables, then build the two bodies:
```powershell
$ING="<ingestion_id>"; $DOC="<document_id>"; $TR="<trace_id>"
$db = Body $ING $DOC $TR      # per document
$ib = IBody $ING $TR          # per ingestion
```
**2-4. Validation, archive (zip only), manifest** (for a ZIP, use each child document id from the manifest for the file stages, and validate each child first):
```powershell
Call validation $db
Call archive $db      # zip only
Call manifest $ib
```
**5-12. The file stages, in this order** (read each result before the next):
```powershell
foreach ($s in "normalization","cleaning","duplicate","extraction","metadata","enrichment","chunking","embedding") { "---- $s"; Call $s $db }
```
For `extraction`, if the answer says `"complete": false`, call it again (scanned PDFs are OCR'd a few pages per call). A duplicate of an earlier upload is reported by `duplicate` and should stop there. `enrichment` never blocks a document: if it fails you can still continue with `chunking`. Optional: `entity-extraction`, then `graph` (NOT_IMPLEMENTED is expected).

**13. Index into the Knowledge Base** (real job, 1-3 minutes):
```powershell
Call vector-indexing $ib
curl.exe -sS -X POST "$B/vector-indexing/api/v1/ingestions/$ING/index/poll" -H $H     # repeat every ~15 s until COMPLETE
```
**14. Reconcile and check the final status:**
```powershell
Call reconciliation $ib
curl.exe -sS "$B/reconciliation/api/v1/ingestions/$ING/reconciliation" -H $H
curl.exe -sS "$B/manifest/api/v1/ingestions/$ING" -H $H
```
Postman: same URLs, header `Authorization: Bearer ...`, upload = POST form-data keys `file` (and optional `owner`), others = POST raw JSON body as in the helper.

## Shortcut helper (same calls, less typing)
```powershell
python scripts\local_step.py upload C:\docs\report.pdf --owner bob   # --owner optional (default ADMIN)
python scripts\local_step.py validate
python scripts\local_step.py manifest
python scripts\local_step.py run normalization      # then cleaning, duplicate, extraction, metadata, enrichment, chunking, embedding, entity-extraction, graph
python scripts\local_step.py index
python scripts\local_step.py reconcile
python scripts\local_step.py status
```
It remembers the ids in `.local-state.json`. `reset` starts a new document.

## See what each stage wrote (S3 keys under your bucket `cleandocs-<account>-eu-west-1`)
```powershell
python scripts\local_step.py show processed/documents/{doc}/manifests/chunks.json
python scripts\local_step.py show processed/documents/{doc}/normalized/normalized.json
python scripts\local_step.py show processed/documents/{doc}/chunks/chunks.json
```
`manifests/chunks.json` lists every chunk and its citation sidecar key (`kb-source/<doc>/<chunk>.txt.metadata.json`).
The `result` of each stage call also names the S3 keys it wrote.

## Notes
- Local only: the secret above is for localhost. Render uses your real `SERVICE_AUTH_SECRET`.
- This uses YOUR AWS permissions, so it does not prove the least-privilege Render keys are enough; the Render smoke test does.
- Data is real (S3, DynamoDB, one Knowledge Base job). Re-uploading identical content is flagged by `duplicate`.
- Port busy: `.\scripts\run_local.ps1 -Port 8002` and `$env:LOCAL_BASE="http://127.0.0.1:8002"` for the helper (and `$B`).
