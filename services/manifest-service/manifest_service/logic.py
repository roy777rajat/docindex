"""Ingestion roll-up: which files to process, per-document state, and the final archive/ingestion status."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import NotFound, ValidationFailure
from cleandocs_shared.models import DocumentStatus, ExecutionStatus, ProcessResponse, TraceContext
from cleandocs_shared.registry import Registry
from cleandocs_shared.rollup import SUCCESS, TERMINAL, rollup  # noqa: F401  (re-exported)


@dataclass
class ManifestDeps:
    settings: Settings
    registry: Registry
    audit: AuditWriter


def _members(deps: ManifestDeps, ingestion_id: str) -> list[dict[str, Any]]:
    if deps.registry.get_ingestion(ingestion_id) is None:
        raise NotFound("ingestion not found", code="INGESTION_NOT_FOUND")
    return deps.registry.list_members(ingestion_id)


def list_files(deps: ManifestDeps, ctx: TraceContext) -> ProcessResponse:
    """Files still to process (role FILE, status PROCESSING). Archive containers are never queued as content."""
    if not ctx.ingestion_id:
        raise ValidationFailure("context.ingestion_id is required", code="INGESTION_ID_REQUIRED")
    with deps.audit.execution(ctx) as rec:
        members = _members(deps, ctx.ingestion_id)
        todo = []
        for m in members:
            if m.get("role", "FILE") != "FILE" or m["status"] != DocumentStatus.PROCESSING.value:
                continue
            doc = deps.registry.get_document(m["document_id"]) or {}
            todo.append({"document_id": m["document_id"], "parent_document_id": doc.get("parent_document_id"),
                         "filename": doc.get("original_filename"), "s3_uri": doc.get("s3_uri_original"),
                         "checksum_sha256": doc.get("checksum_sha256")})
        todo.sort(key=lambda f: f["document_id"])
        rec.add_metric("files_to_process", len(todo))
        return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
                               metrics={"files_to_process": len(todo)}, result={"files": todo, **rollup(members)})


def set_state(deps: ManifestDeps, ingestion_id: str, document_id: str, status: str,
              stage: str | None = None, error_code: str | None = None) -> dict[str, Any]:
    if status not in {s.value for s in DocumentStatus}:
        raise ValidationFailure("unknown status", code="STATUS_INVALID")
    members = {m["document_id"]: m for m in _members(deps, ingestion_id)}
    if document_id not in members:
        raise NotFound("document is not a member of this ingestion", code="MEMBER_NOT_FOUND")
    if members[document_id]["status"] in TERMINAL and members[document_id]["status"] != status:
        raise ValidationFailure("document already in a terminal state", code="STATE_CONFLICT")
    fields: dict[str, Any] = {"status": status}
    if error_code:
        fields["error_code"] = error_code
    deps.registry.update_member(ingestion_id, document_id, **fields)
    dfields = dict(fields, **({"stage": stage} if stage else {}))
    deps.registry.update_document(document_id, **dfields)
    return {"document_id": document_id, "status": status}


def ingestion_status(deps: ManifestDeps, ingestion_id: str) -> dict[str, Any]:
    members = _members(deps, ingestion_id)
    reconciled = bool((deps.registry.get_ingestion(ingestion_id) or {}).get("reconciled_at"))
    r = rollup(members)
    # "settled" = every file is terminal or waiting for the knowledge-base job (INDEX_READY): the workflow's cue to index.
    ready = pending = 0
    for m in members:
        if m.get("role", "FILE") != "FILE" or m["status"] != "PROCESSING":
            continue
        doc = deps.registry.get_document(m["document_id"]) or {}
        if doc.get("stage") == "INDEX_READY" and doc.get("indexing_state") in (None, "PENDING"):
            ready += 1
        else:
            pending += 1
    r.update(index_ready_files=ready, pending_files=pending, all_settled=bool(r["total_files"]) and pending == 0)
    if r["all_terminal"] and not reconciled:  # once reconciled, the evidence-based status is final
        deps.registry.update_ingestion(ingestion_id, status=r["status"])
        for a in r["archives"]:
            deps.registry.update_member(ingestion_id, a, status=r["status"])
            deps.registry.update_document(a, status=r["status"])
    return {"ingestion_id": ingestion_id, **r}
