# Citation-ready indexing (requirement, not yet queried)

Querying is out of scope for this phase, but every indexed chunk must let a future query return a citation for the
retrieved text. Indexing therefore stores provenance now.

## Chunk contract (enforced by chunking-service in Phase 5)
One S3 object = one chunk: `kb-source/{document_id}/{chunk_id}.txt`, with sidecar `{chunk_id}.txt.metadata.json`.

Sidecar attributes (short keys, well under the Bedrock ~1 KB / 35-key metadata limit):

| key | meaning |
|---|---|
| `document_id`, `chunk_id`, `chunk_index` | stable identity; chunk_id is deterministic (document + index + text hash) |
| `filename` | original filename of the document |
| `parent_document_id`, `archive_entry_path` | set for files extracted from a ZIP, so the citation names the archive entry |
| `page_start`, `page_end` | page range (PDF/images/TIFF) when known |
| `section` | heading path where the chunk starts, truncated to 120 chars, when known |
| `ingestion_id`, `doc_type` | filtering |

Full detail (character offsets, bounding boxes, extraction method) lives in
`processed/documents/{id}/chunks/chunks.json` and the registry, resolved by `document_id` + `chunk_id`.

## Rules
- Chunks never span documents; page/section boundaries are recorded, never guessed.
- Missing locators are omitted, not invented.
- Deletion removes the objects and re-syncs, so citations never point at removed content.
- A future query returns `{document_id, chunk_id, filename, archive_entry_path, page_start..page_end, section}` with each hit.

## As built (Phase 5)
- `chunking-service` writes staging `processed/documents/{id}/chunks/chunks.json` (text + locators). It never writes to `kb-source/`.
- `embedding-service` validates, then publishes `kb-source/{id}/{chunk_id}.txt` + sidecar, removes stale chunk objects of the document,
  and writes `manifests/chunks.json` (pointers, hashes, locators, no text). Sidecar budget: 1,500 bytes.
- `source_s3_uri` and the checksum are not in the sidecar: Bedrock already returns the chunk's S3 URI, and the original file is
  resolved from the registry with `document_id`.
- `sheet` is recorded for spreadsheet chunks; `section` is where the chunk starts (a chunk may run into the next heading).
- `vector-indexing-service` marks a document COMPLETED only when every one of its chunk objects is reported INDEXED by the KB.
