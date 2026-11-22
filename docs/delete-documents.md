# Delete documents (one, many, or everything)

[Back to the handbook index](README.md)

Runs on your PC with **your own AWS admin credentials** (`aws configure`), never the Render keys. Needs `uv`. Run from `E:\dev\docindex`.
Infrastructure (bucket, tables, Knowledge Base, Render) is never deleted by these commands.

## Commands
```powershell
.\scripts\cleandocs.ps1 list                        # every document: id, status, stage, owner, filename, ingestion id
.\scripts\cleandocs.ps1 delete doc_xxxxxxxx -DryRun # show what WOULD be deleted, delete nothing
.\scripts\cleandocs.ps1 delete doc_xxxxxxxx         # delete one document
.\scripts\cleandocs.ps1 delete doc_a doc_b          # delete several
.\scripts\cleandocs.ps1 delete ing_xxxxxxxx         # delete a whole upload (all files of that ingestion)
.\scripts\cleandocs.ps1 clean-dry                   # count everything that delete-all would remove (deletes nothing)
.\scripts\cleandocs.ps1 delete-all                  # delete EVERYTHING; you must type  DELETE ALL
```
Where to get an id: `list` output, or the `document_id` / `ingestion_id` in your upload answer.

## What happens
| Command | Removes |
|---|---|
| `delete <doc>` | S3: `raw/`, `processed/`, `kb-source/` of that document (every version). Registry: document row, membership, duplicate-checksum claim, the upload's idempotency key. Audit trail rows of that document. Then a Knowledge Base sync removes its vectors (1-3 min). |
| `delete <zip doc>` | the ZIP **and every file extracted from it** |
| `delete <ing_...>` | all documents of that upload, then the ingestion rows themselves |
| `delete-all` | all of the above for everything: S3 `raw/ processed/ kb-source/ tmp/`, registry rows (except monthly cost counters), all audit rows, all vectors. Checks that the vector index is empty at the end and prints `CLEAN` |

Because the duplicate claim and idempotency key are removed too, **you can upload the same file again straight away** and it is processed as new (not `DUPLICATE`).

## Safety rules built in
- Refuses to run while any Step Functions workflow is RUNNING (wait or stop it in the console).
- `-DryRun` first is recommended for anything except a test file.
- `delete-all` needs the typed phrase. The lower-level tool `python scripts\clean_all.py` is a dry run unless you add `--yes DELETE-ALL-<your account id>`; add `--reset-budgets` to also reset the monthly Textract and enrichment counters.
- Deleting a file that was the "original" of a duplicate leaves the duplicate pointing at a missing original: delete by `ing_...` (the whole upload) when in doubt.

## Re-ingest after a delete
1. `.\scripts\cleandocs.ps1 delete <id>` (wait for the Knowledge Base sync line).
2. `.\scripts\warm-up.ps1`, then upload again with a **new** `Idempotency-Key` ([Operator handbook](operator-handbook.md), section 3).

## Check afterwards
- `.\scripts\cleandocs.ps1 list` no longer shows it.
- Bedrock console -> Knowledge base -> Test: the text is no longer found (after the sync).
- Optional: `aws s3 ls s3://<bucket>/processed/documents/<doc_id>/` returns nothing.

## If something fails
| Message | Meaning |
|---|---|
| `cannot read stack 'cleandocs-core'` | wrong AWS account/region in your CLI profile (`aws sts get-caller-identity`) |
| `Step Functions executions are RUNNING` | a workflow is still running; wait or stop it |
| `NOT clean: ...` after delete-all | the Knowledge Base sync did not finish; run the sync in the Bedrock console (Knowledge base -> Data source -> Sync), then `clean-dry` again |
| `not found in registry: doc_x` | the registry row was already gone; leftover S3 files for that id are still removed |
