# CleanDocs AI - project state (read this first)

[Back to the handbook index](README.md)

Last updated: 2026-11-22. Owner: Rajat. Repo: https://github.com/roy777rajat/docindex (local `E:\dev\docindex`).
Purpose of this file: one place that tells a new reader (or a new Claude session) what exists, how it fits together, what was
decided, what is unfinished and what to improve. Companion docs are listed at the end.

## 1. What CleanDocs AI is
A personal, low-cost document ingestion and indexing platform. You upload a PDF, image, Word, Excel, text file or a ZIP of those.
It validates, converts, cleans, OCRs (scans), classifies, labels with a small LLM, splits into chunks and indexes them into an
Amazon Bedrock Knowledge Base (vectors in S3 Vectors) with citation metadata. **There is no chatbot / search API yet** (planned:
`query-api`). Everything is built to be searched later by meaning AND by metadata (owner, type, tags, date, filename...).

## 2. Architecture in one page
```
you (curl/Postman) --> upload-service (Render) --> S3 raw/ + DynamoDB registry --> Step Functions "cleandocs-ingestion"
   ingestion workflow: validation(root) -> archive (ZIP only) -> manifest -> wait until all files settle
                       -> vector-indexing (Bedrock sync job) -> reconciliation (evidence report)
   per-file workflow "cleandocs-file" (one file at a time through a DynamoDB gate; dispatcher Lambda every 5 min):
     validation -> normalization -> cleaning -> duplicate -> extraction (OCR/Textract) -> metadata -> enrichment (LLM, soft)
     -> chunking -> embedding (writes kb-source/ chunk + sidecar) -> [entity-extraction -> graph: optional, never fail a file]
```
- **16 FastAPI services** on Render Free (Docker, Frankfurt), one per stage, each `https://cleandocs-<name>-service.onrender.com`.
  Called only by Step Functions (and by you with the bearer secret). No Swagger pages anywhere (disabled on purpose).
- **AWS (eu-west-1)**: S3 bucket, SQS file queue + DLQ, DynamoDB `cleandocs-registry` and `document-ingest-trail`, Step Functions x2,
  dispatcher Lambda + EventBridge schedule, EventBridge Connection (adds the bearer header), Bedrock Knowledge Base, S3 Vectors
  bucket + index, 3 IAM users (tiers), roles, log groups. Textract (OCR) and Bedrock models are called by services.
- **Auth**: every call except `/health/*` needs `Authorization: Bearer <SERVICE_AUTH_SECRET>` (same value in Render env group
  `cleandocs-auth` and in the orchestration stack parameter). Services fail closed (503) if the secret is not configured.
- **Least privilege**: three AWS keys (IAM users) - `ingest` (upload, archive), `processing` (most stages, Textract, Bedrock invoke),
  `indexing` (vector-indexing, reconciliation). Render has only these keys, never your admin credentials.

## 3. The 16 services
| # | Service | Job |
|---|---|---|
| 1 | upload | Receive file (`owner`, `label`, `Idempotency-Key`), hash, store original, register, start the workflow |
| 2 | validation | Magic-byte/type/size/page checks (never trusts the extension) |
| 3 | archive | Unzip safely (zip-bomb, count, depth limits); children inherit the ZIP's owner |
| 4 | manifest | List files of an ingestion, status roll-up (`GET /api/v1/ingestions/{id}`) |
| 5 | normalization | PDF/DOCX/XLSX/CSV/TXT/image -> page/block JSON; flags scanned pages needing OCR |
| 6 | cleaning | Text clean-up (whitespace, hyphenation, control chars) |
| 7 | duplicate | Exact duplicate by SHA-256 (policy SKIP/LINK/REPROCESS) |
| 8 | extraction | Keep text-layer pages; OCR scanned pages with Textract (few pages per call, monthly cap); saves average OCR confidence |
| 9 | metadata | Rules: title, doc type guess, language hint, dates, keywords |
| 10 | enrichment | One Amazon Nova Micro call: doc_type, tags, description, title, language, date, PII flag (soft stage) |
| 11 | chunking | Layout-aware chunks (headings, tables by rows with header repeated, paragraph split, overlap) |
| 12 | embedding | Validates chunks, writes `kb-source/<doc>/<chunk>.txt` + `.metadata.json` sidecar (Bedrock creates the embeddings) |
| 13 | vector-indexing | Starts/polls the Bedrock ingestion job for the ingestion |
| 14 | reconciliation | Evidence-based final status per document/ingestion (S3 objects, KB status, sidecar owner match) |
| 15 | entity-extraction | Optional regex entity candidates (never blocks) |
| 16 | graph | Skeleton only (`NOT_IMPLEMENTED`) |

## 4. Data and metadata
- **S3 layout** (bucket `cleandocs-<account>-eu-west-1`): `raw/documents/<doc>/original/<file>` (immutable originals),
  `processed/documents/<doc>/<stage>/...` (every stage output, incl. `extraction/raw/textract_p*.json`, `metadata/enrichment.json`,
  `chunks/chunks.json`, `manifests/chunks.json`), `kb-source/<doc>/` (what Bedrock indexes), `tmp/`, `deploy/` (workflow packages).
- **Registry rows**: `DOC#<id>/META`, `INGEST#<ing>/META`, `INGEST#<ing>/DOC#<id>` (membership), `CHECKSUM#<sha>/CLAIM` (duplicate claim),
  `IDEMP#<key>/CLAIM` (idempotency), `GATE#GLOBAL/LOCK`, `BUDGET#<yyyy-mm>/COUNTERS` (Textract pages, enrichment docs).
- **Trail** (`document-ingest-trail`): one STARTED and one COMPLETED row per service call per document (append-only audit).
- **Chunk metadata (sidecar, max 1,500 bytes)**: `document_id, chunk_id, chunk_index, filename, ingestion_id, doc_type, owner,
  file_kind, language, uploaded_at (epoch s), ocr_confidence, ocr_low_confidence, doc_date, tags[], has_pii, parent_document_id,
  archive_entry_path, page_start, page_end, sheet, section`. Details: `docs/citations.md`.
- **Owner model**: `owner` form field at upload, default `ADMIN` (reserved). Others are lower-cased labels (1-64 of `A-Za-z0-9._@-`).
  Single owner per document. The future query-api must enforce: `ADMIN` sees all, others only `owner == user`. Today it is only a label.

## 5. What is DONE (all in Git; 175 automated tests pass on mocked AWS)
| Phase | Result |
|---|---|
| 0-1 | Architecture, shared library (config, errors, audit, registry, pipeline, auth) |
| 2 | AWS core stack `cleandocs-core` deployed; KB smoke test passed |
| 3-6 | Ingestion, processing, chunking/embedding/indexing services, graph skeleton |
| 7 | Orchestration stack `cleandocs-orchestration` (gate, dispatcher, 2 state machines) deployed |
| 8 | Reconciliation (evidence-based statuses) |
| 9 | Render Blueprint, deploy scripts, runbook; 15 services deployed and verified end to end (smoke test, 3-PDF ZIP COMPLETED) |
| 10A | `owner` at upload (ZIP children inherit); richer chunk metadata; reconciliation checks `sidecar_owner_matches_registry`, `sidecar_attributes_present` |
| 10B | enrichment-service (Nova Micro `eu.amazon.nova-micro-v1:0`), soft stage before chunking, monthly cap 1,000 docs, tags/doc_date/has_pii in chunks |
| 10-extra | OCR confidence + `ocr_low_confidence` (<80) saved; admin tools (`cleandocs.ps1`: list, delete one, delete all), `warm-up.ps1`, `show-config.ps1`, `teardown-account.ps1` |
| Verified live | After 10A/10B deploy: 2-file ZIP with `owner=bob` COMPLETED, reconciliation OK, Enrichment step ran |

## 6. What is PENDING (planned, approved, not built)
| Item | Notes |
|---|---|
| 10C registry GSI `owner-index` | Fast "list documents of owner X" (`GSI1PK=OWNER#<owner>`, `GSI1SK=<uploaded_at>#<doc_id>`); one GSI per CloudFormation update |
| 10D deletion/purge **service** | Today covered by scripts (`cleandocs.ps1`). Service version = `POST /purge` with dry-run default, confirm phrase `DELETE-ALL-<account-id>`; would run on a widened `indexing` IAM tier (no separate tier, decided) |
| query-api (RAG chatbot) | Not started. Needs owner enforcement, metadata filters, citations, rerank, answer model |
| Redeploy checks | `scripts/deploy-render.ps1` was never run against Render (use Manual Deploy if it fails) |

## 7. Known limitations and ENHANCEMENTS (decided "not now", revisit with the query-api)
1. **Scanned tables**: OCR returns flat lines. Textract table analysis (AnalyzeDocument TABLES) would keep rows/cells (e.g. grade cards,
   bank statements). Costs more per page; needs a higher `TEXTRACT_MONTHLY_PAGE_CAP`.
2. **Row-aware chunking for scanned tables** once (1) exists.
3. **Watermarks**: repeated background words (e.g. "Utech" ~40 times on one certificate) pollute text. Cheap fix: drop short lines that repeat >=5 times per page.
4. **Parent-child retrieval** (small chunks to match, parent section as context): option A switch the KB to hierarchical chunking (full re-index), option B keep our chunks and store section text in S3 for the chatbot.
5. **Handwriting**: plain text detection only; untested - test with a real sample.
6. **Images inside Word/PDF**: not OCR'd. 7. **Formats**: `.pptx`, `.html`, `.md` not accepted; old `.doc` recognised but not processed.
8. **OCR confidence** hides local damage (a grade card scored 96 with two damaged rows).
9. Graph stage is a skeleton. 10. Replay tool stops at INDEX_READY. 11. Metadata-service language/doc-type rules are weak for Hindi (enrichment helps).
12. Sidecar size (1,500 B) and S3 Vectors 2 KB filterable limit not tested at the maximum against real S3 Vectors.

## 8. Operating facts (this deployment)
| Thing | Value (account <ACCOUNT_ID>, eu-west-1) |
|---|---|
| Bucket | `cleandocs-<ACCOUNT_ID>-eu-west-1` |
| Tables | `cleandocs-registry`, `document-ingest-trail` |
| Knowledge Base / data source | `<KB_ID>` / `<DS_ID>` (always readable with `.\scripts\show-config.ps1`) |
| Vector bucket / index | `cleandocs-vectors-<ACCOUNT_ID>` / `cleandocs-index` (Titan v2, 1024-dim, cosine) |
| State machines | `cleandocs-ingestion`, `cleandocs-file`; Lambda `cleandocs-dispatcher` |
| Stacks | `cleandocs-core`, `cleandocs-orchestration` |
| Render | Blueprint `cleandocs`; env groups `cleandocs-auth`, `cleandocs-aws-ingest|processing|indexing`, `cleandocs-config` |
| Costs to watch | Textract (cap 300 pages/month), Bedrock embeddings + Nova Micro (cap 1,000 docs/month), S3 Vectors, Step Functions, Secrets Manager (EventBridge connection), CloudWatch logs |

The repo is public: it shows the account id, bucket, KB id and admin user name, but **no secrets**. Make it private if you can.

## 9. Working agreement (how Claude and Rajat work)
Plan is reviewed with Rajat before a phase is applied; approval between phases; no surprise AWS spend; ask for any value Claude cannot find.
Windows + PowerShell + VS Code + `uv`; Claude mirrors files to `E:\dev\docindex`; Rajat pushes to Git.

## 10. Where to look next
Open [README.md](README.md) (the handbook index). It links: (a) operator handbook, (b) delete assets from the AWS account, (c) new AWS account setup,
(d) delete documents, (e) deploy to Render and push to Git, plus the reference docs.
