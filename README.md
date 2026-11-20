# CleanDocs AI

Modular document ingestion and indexing platform on AWS (home-lab, low cost).
Scope: ingest, validate, extract, normalize, clean, classify, chunk and index documents into
Amazon Bedrock Knowledge Bases on S3 Vectors. **No chatbot or retrieval API in this phase.**

Design: [`docs/phase0-architecture.md`](docs/phase0-architecture.md)

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Architecture and design | Approved |
| 1 | Shared foundation (`shared/`) | Built, 40 unit tests passing |
| 2 | AWS infrastructure | Deployed (`cleandocs-core` stack); KB smoke test pending, see `docs/phase2-infrastructure.md` |
| 3 | Ingestion: upload, validation, archive, manifest | Built, tested on mocks |
| 4 | Processing: normalization, cleaning, duplicate, extraction (OCR), metadata | Built, tested on mocks |
| 5 | Chunking, embedding prep, vector indexing (citation-ready) | Built, tested on mocks |
| 6 | Knowledge-graph skeleton (`NOT_IMPLEMENTED`) | Built |
| 7 | Orchestration: gate, dispatcher, two Step Functions workflows | Deployed (`cleandocs-orchestration` stack); not yet run against live services, see `docs/phase7-orchestration.md` |
| 8 | Reconciliation (evidence-based statuses) | Built, tested on mocks, see `docs/phase8-reconciliation.md` |
| 9 | Render blueprint, deploy guide, smoke test, replay tool, runbook | Built; Render deploy in progress, see `docs/deploy-render.md` and `docs/operations.md` |

## Quick start (Windows PowerShell)

```powershell
.\scripts\dev-setup.ps1
```

Unit tests use `moto` mocks and never touch real AWS.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\.venv\Scripts\python.exe -m pytest tests -q --import-mode=importlib       # all mocked tests
.\.venv\Scripts\python.exe scripts\local_e2e.py                            # whole pipeline on YOUR machine against REAL AWS
```
Want to run all services on one local port (8001) and test stage by stage with curl? See [`docs/local-testing.md`](docs/local-testing.md).

`local_e2e.py` calls the 15 service apps in-process in workflow order (no Render, no Step Functions) and prints the reconciliation report.

## Layout

`shared/` (versioned foundation package) · `services/` · `workers/` · `orchestration/` ·
`infrastructure/` · `tests/` · `scripts/` · `docs/`


## Citations
Indexed chunks carry citation provenance (document, filename, archive entry, pages, section). See [docs/citations.md](docs/citations.md).
