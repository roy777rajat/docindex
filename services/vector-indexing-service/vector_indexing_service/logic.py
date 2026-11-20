"""vector-indexing-service: one Knowledge Base ingestion job per ingestion batch, then per-document verification.

Step Functions drives it without holding an HTTP request open:
  POST /api/v1/process                         start the job for all INDEX_READY files of the ingestion (idempotent)
  POST /api/v1/ingestions/{id}/index/poll      check the job; when COMPLETE, verify every chunk and finalize documents
Documents are marked COMPLETED only if every one of their chunks is reported INDEXED by the Knowledge Base.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from cleandocs_shared import utils
from cleandocs_shared.kb import kb_document_statuses
from cleandocs_shared.exceptions import NotFound, ServiceUnavailable, ValidationFailure, classify_exception
from cleandocs_shared.models import DocumentStage, DocumentStatus, ExecutionStatus, ProcessRequest, ProcessResponse, TraceContext
from cleandocs_shared.pipeline import StageDeps, read_object, stage_key

RUNNING = {"STARTING", "IN_PROGRESS"}


def _kb(deps: StageDeps) -> tuple[str, str]:
    kb, ds = deps.settings.bedrock_knowledge_base_id, deps.settings.bedrock_data_source_id
    if not kb or not ds:
        raise ValidationFailure("knowledge base is not configured", code="KB_NOT_CONFIGURED")
    return kb, ds


def _ready_documents(deps: StageDeps, ingestion_id: str) -> list[dict[str, Any]]:
    if deps.registry.get_ingestion(ingestion_id) is None:
        raise NotFound("ingestion not found", code="INGESTION_NOT_FOUND")
    out = []
    for m in deps.registry.list_members(ingestion_id):
        if m.get("role", "FILE") != "FILE" or m["status"] != DocumentStatus.PROCESSING.value:
            continue
        doc = deps.registry.get_document(m["document_id"])
        if doc and doc.get("stage") == DocumentStage.INDEX_READY.value:
            out.append(doc)
    return sorted(out, key=lambda d: d["document_id"])


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    ctx = req.context
    if not ctx.ingestion_id:
        raise ValidationFailure("context.ingestion_id is required", code="INGESTION_ID_REQUIRED")
    kb, ds = _kb(deps)
    with deps.audit.execution(ctx) as rec:
        ing = deps.registry.get_ingestion(ctx.ingestion_id)
        if ing is None:
            raise NotFound("ingestion not found", code="INGESTION_NOT_FOUND")
        if ing.get("kb_job_id") and ing.get("kb_job_status") in RUNNING:  # repeat call: do not start a second job
            rec.skip("a knowledge base job is already running for this ingestion")
            return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                                   skipped_reason=rec.skipped_reason,
                                   result={"job_id": ing["kb_job_id"], "state": "RUNNING", "documents": ing.get("kb_job_documents", 0)})
        docs = _ready_documents(deps, ctx.ingestion_id)
        if not docs:
            rec.skip("no documents are ready for indexing")
            return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                                   skipped_reason=rec.skipped_reason, result={"job_id": None, "state": "NOTHING_TO_INDEX", "documents": 0})
        token = hashlib.sha256((ctx.ingestion_id + "|" + ",".join(d["document_id"] for d in docs)).encode()).hexdigest()
        try:
            job = deps.bedrock_agent.start_ingestion_job(
                knowledgeBaseId=kb, dataSourceId=ds, clientToken=token, description=f"cleandocs {ctx.ingestion_id}"[:200],
            )["ingestionJob"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConflictException":
                raise ServiceUnavailable("another knowledge base job is running; retry later", code="KB_JOB_ALREADY_RUNNING") from exc
            raise classify_exception(exc) from exc
        deps.registry.update_ingestion(ctx.ingestion_id, kb_job_id=job["ingestionJobId"], kb_job_status=job["status"],
                                       kb_job_documents=len(docs), indexing_started_at=utils.iso())
        for d in docs:
            deps.registry.update_document(d["document_id"], indexing_state="INDEXING", kb_job_id=job["ingestionJobId"])
        rec.add_metric("documents", len(docs))
        return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
                               metrics={"documents": len(docs)},
                               result={"job_id": job["ingestionJobId"], "state": "RUNNING", "documents": len(docs)})


def _kb_statuses(deps: StageDeps, kb: str, ds: str) -> dict[str, str]:
    return kb_document_statuses(deps.bedrock_agent, kb, ds)


def _fail(deps: StageDeps, doc: dict[str, Any], code: str, reason: str) -> None:
    deps.registry.update_document(doc["document_id"], status=DocumentStatus.FAILED.value, indexing_state="FAILED",
                                  error_code=code, error_message=reason[:200])
    deps.registry.update_member(doc["ingestion_id"], doc["document_id"], status=DocumentStatus.FAILED.value, error_code=code)


def poll(deps: StageDeps, ingestion_id: str) -> dict[str, Any]:
    kb, ds = _kb(deps)
    ing = deps.registry.get_ingestion(ingestion_id)
    if ing is None:
        raise NotFound("ingestion not found", code="INGESTION_NOT_FOUND")
    job_id = ing.get("kb_job_id")
    if not job_id:
        raise NotFound("no knowledge base job has been started for this ingestion", code="KB_JOB_NOT_STARTED")
    ctx = TraceContext(ingestion_id=ingestion_id)
    with deps.audit.execution(ctx) as rec:
        try:
            job = deps.bedrock_agent.get_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds, ingestionJobId=job_id)["ingestionJob"]
        except ClientError as exc:
            raise classify_exception(exc) from exc
        status = job["status"]
        deps.registry.update_ingestion(ingestion_id, kb_job_status=status)
        docs = [d for d in (deps.registry.get_document(m["document_id"]) for m in deps.registry.list_members(ingestion_id)
                            if m.get("role", "FILE") == "FILE") if d and d.get("kb_job_id") == job_id
                and d.get("indexing_state") == "INDEXING"]
        stats = job.get("statistics", {})
        if status in RUNNING:
            return {"job_id": job_id, "state": "RUNNING", "documents_waiting": len(docs)}
        if status != "COMPLETE":
            reasons = "; ".join(job.get("failureReasons", []))[:200] or status
            for d in docs:
                _fail(deps, d, "KB_JOB_FAILED", reasons)
            rec.add_metric("documents_failed", len(docs))
            return {"job_id": job_id, "state": "FAILED", "documents_failed": len(docs), "reason": reasons}
        kb_status = _kb_statuses(deps, kb, ds)
        indexed = failed = 0
        for d in docs:
            raw = read_object(deps, stage_key(d["document_id"], "manifests", "chunks.json"))
            uris = [c["s3_uri"] for c in json.loads(raw)["data"]["chunks"]] if raw else []
            bad = [u for u in uris if kb_status.get(u) != "INDEXED"]
            if not uris or bad:
                _fail(deps, d, "CHUNKS_NOT_INDEXED", f"{len(bad) or 'all'} of {len(uris)} chunks not indexed")
                failed += 1
                continue
            deps.registry.update_document(d["document_id"], stage=DocumentStage.INDEXED.value, indexing_state="INDEXED",
                                          status=DocumentStatus.COMPLETED.value, indexed_at=utils.iso(), kb_chunk_count=len(uris))
            deps.registry.update_member(d["ingestion_id"], d["document_id"], status=DocumentStatus.COMPLETED.value)
            indexed += 1
        rec.add_metric("documents_indexed", indexed)
        rec.add_metric("documents_failed", failed)
        return {"job_id": job_id, "state": "COMPLETE", "documents_indexed": indexed, "documents_failed": failed,
                "kb_statistics": {k: v for k, v in stats.items() if isinstance(v, int)}}


class PollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def add_routes(app: FastAPI, deps: StageDeps, auth: Callable[..., None]) -> None:
    @app.post("/api/v1/ingestions/{ingestion_id}/index/poll", dependencies=[Depends(auth)])
    def poll_route(ingestion_id: str) -> dict[str, Any]:
        return poll(deps, ingestion_id)
