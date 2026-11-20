# Run all services on ONE local port and test them one by one

One FastAPI process on `http://127.0.0.1:8001` hosts all 15 services. Each service lives under its own path prefix:
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
| # | Service | Path prefix | Swagger page | Main call |
|---|---|---|---|---|
| 1 | upload | `/upload` | `/upload/docs` | `POST /upload/api/v1/uploads` (multipart file) |
| 2 | validation | `/validation` | `/validation/docs` | `POST /validation/api/v1/process` |
| 3 | archive (zip only) | `/archive` | `/archive/docs` | `POST /archive/api/v1/process` |
| 4 | manifest | `/manifest` | `/manifest/docs` | `POST /manifest/api/v1/process`, `GET /manifest/api/v1/ingestions/{ingestion_id}` |
| 5 | normalization | `/normalization` | `/normalization/docs` | `POST .../api/v1/process` |
| 6 | cleaning | `/cleaning` | | same |
| 7 | duplicate | `/duplicate` | | same |
| 8 | extraction (OCR here) | `/extraction` | | same (repeat while `complete` is false) |
| 9 | metadata | `/metadata` | | same |
| 10 | chunking | `/chunking` | | same |
| 11 | embedding | `/embedding` | | same |
| 12 | vector-indexing | `/vector-indexing` | | `POST .../api/v1/process` start, then `POST .../api/v1/ingestions/{id}/index/poll` |
| 13 | reconciliation | `/reconciliation` | | `POST .../api/v1/process`, `GET .../api/v1/ingestions/{id}/reconciliation` |
| 14 | entity-extraction (optional) | `/entity-extraction` | | same |
| 15 | graph (skeleton) | `/graph` | | same, returns NOT_IMPLEMENTED |

`http://127.0.0.1:8001/` lists them all. Every service also has `/health/live` and `/health/ready`, and `/api/v1/executions` (audit trail).

## Walk one PDF through (curl; use `curl.exe` in PowerShell)
Set once:
```powershell
$H = "Authorization: Bearer local-dev-secret-change-me-0123456789"
$B = "http://127.0.0.1:8001"
```
**1. Upload** (note `ingestion_id`, `document_id`, `trace_id` in the answer):
```powershell
curl.exe -s -X POST "$B/upload/api/v1/uploads" -H $H -H "Idempotency-Key: t1" -F "file=@C:\docs\report.pdf"
```
Put the ids in variables (replace the values):
```powershell
$ING="<ingestion_id>"; $DOC="<document_id>"; $TR="<trace_id>"
```
Every later stage takes the same body. The per-document one:
```powershell
$body = "{`"context`":{`"ingestion_id`":`"$ING`",`"document_id`":`"$DOC`",`"trace_id`":`"$TR`",`"workflow_execution_id`":`"local`"},`"parameters`":{}}"
```
**2-4. Validation, archive (only for a zip), manifest:**
```powershell
curl.exe -s -X POST "$B/validation/api/v1/process" -H $H -H "Content-Type: application/json" -d $body
curl.exe -s -X POST "$B/archive/api/v1/process"    -H $H -H "Content-Type: application/json" -d $body   # zip only
$ibody = "{`"context`":{`"ingestion_id`":`"$ING`",`"trace_id`":`"$TR`",`"workflow_execution_id`":`"local`"},`"parameters`":{}}"
curl.exe -s -X POST "$B/manifest/api/v1/process" -H $H -H "Content-Type: application/json" -d $ibody
```
**5-11. The file stages, in this order** (run each, read the result, then the next):
```powershell
foreach ($s in "normalization","cleaning","duplicate","extraction","metadata","chunking","embedding") {
  "---- $s"; curl.exe -s -X POST "$B/$s/api/v1/process" -H $H -H "Content-Type: application/json" -d $body
}
```
Do them one at a time by hand if you want to inspect in between. For `extraction`, if the answer says `"complete": false`, call it again
(large scanned PDFs are OCR'd a few pages per call). For `duplicate`, a duplicate of an earlier upload is reported and should stop there.
Optional: `entity-extraction`, then `graph` (NOT_IMPLEMENTED is expected).

**12. Index into the Knowledge Base** (real job, 1-3 minutes):
```powershell
curl.exe -s -X POST "$B/vector-indexing/api/v1/process" -H $H -H "Content-Type: application/json" -d $ibody
curl.exe -s -X POST "$B/vector-indexing/api/v1/ingestions/$ING/index/poll" -H $H     # repeat every ~15 s until COMPLETE
```
**13. Reconcile and check the final status:**
```powershell
curl.exe -s -X POST "$B/reconciliation/api/v1/process" -H $H -H "Content-Type: application/json" -d $ibody
curl.exe -s "$B/reconciliation/api/v1/ingestions/$ING/reconciliation" -H $H
curl.exe -s "$B/manifest/api/v1/ingestions/$ING" -H $H
```
Postman: same URLs, header `Authorization: Bearer ...`, upload = POST form-data key `file`, others = POST raw JSON body as above.
Or open `http://127.0.0.1:8001/<service>/docs`, click Authorize/enter the header, and use "Try it out".

## Shortcut helper (same calls, less typing)
```powershell
python scripts\local_step.py upload C:\docs\report.pdf
python scripts\local_step.py validate
python scripts\local_step.py manifest
python scripts\local_step.py run normalization      # then cleaning, duplicate, extraction, metadata, chunking, embedding, entity-extraction, graph
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
