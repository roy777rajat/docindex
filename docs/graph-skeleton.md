# Knowledge graph skeleton (Phase 6)

**Status: NOT_IMPLEMENTED by design.** There is no graph database, no Neptune, and no DynamoDB-as-graph.
`graph_status` is tracked separately from the document status, never blocks `COMPLETED`, and is never `COMPLETED`.

## What exists
- Contracts: `shared/cleandocs_shared/graph_models.py` (`EvidenceRef`, `Entity`, `Relationship`); JSON Schemas in
  `docs/schemas/` (regenerate with `python scripts/export_schemas.py`). Every entity and relationship needs at least one
  `EvidenceRef` (document, chunk ids, block, page/sheet/section, character span, snippet, archive entry path), so graph facts stay citable.
- **entity-extraction-service** (optional branch, non-blocking): regex candidates for EMAIL, MONEY, DATE, IDENTIFIER,
  ORGANIZATION, PERSON, grouped per document with up to 5 evidence items each, written to
  `processed/documents/{id}/graph-candidates/entities.json`. These are suggestions, not facts. A failure never fails the document.
- **graph-service**: `POST /api/v1/process` writes a `NOT_IMPLEMENTED` audit event, sets `graph_status=NOT_IMPLEMENTED`, and answers 200 so
  the workflow continues. `POST /api/v1/graph/{resolve-entities,extract-relationships,persist,validate}` answer HTTP 501
  `{"status":"NOT_IMPLEMENTED",...}` and audit each call.

## What does not exist
Entity resolution across documents, relationship extraction, persistence and validation. Candidates are deterministic regex
only (no model calls); organisation and person patterns will produce false positives and misses.
