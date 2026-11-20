"""graph-service: contract skeleton. There is no graph database; every operation reports NOT_IMPLEMENTED.

- POST /api/v1/process (workflow branch): records a NOT_IMPLEMENTED audit event and sets graph_status, answers 200 so the
  workflow continues. The document is never held back and graph_status is never COMPLETED.
- POST /api/v1/graph/{resolve-entities,extract-relationships,persist,validate}: HTTP 501 with the contract body.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from cleandocs_shared.graph_models import GraphRequest, NotImplementedBody
from cleandocs_shared.models import ExecutionStatus, GraphStatus, ProcessRequest, ProcessResponse, TraceContext
from cleandocs_shared.pipeline import StageDeps, load_document

OPERATIONS = ("resolve-entities", "extract-relationships", "persist", "validate")


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    with deps.audit.execution(ctx) as rec:
        rec.not_implemented()
        deps.registry.update_document(ctx.document_id, graph_status=GraphStatus.NOT_IMPLEMENTED.value)
        return ProcessResponse(
            service_execution_id=rec.service_execution_id, status=ExecutionStatus.NOT_IMPLEMENTED,
            skipped_reason="knowledge graph is not implemented", result={"graph_status": GraphStatus.NOT_IMPLEMENTED.value},
        )


def add_routes(app: FastAPI, deps: StageDeps, auth: Callable[..., None]) -> None:
    def make(op: str) -> None:
        @app.post(f"/api/v1/graph/{op}", dependencies=[Depends(auth)], status_code=501, name=op)
        def contract(body: GraphRequest) -> JSONResponse:
            ctx = TraceContext(document_id=body.document_id, ingestion_id=body.ingestion_id)
            with deps.audit.execution(ctx) as rec:
                rec.not_implemented()
                rec.add_metric("operation", op)
                rec.add_metric("entities", len(body.entities))
            return JSONResponse(status_code=501, content=NotImplementedBody(operation=op).model_dump())

    for op in OPERATIONS:
        make(op)
