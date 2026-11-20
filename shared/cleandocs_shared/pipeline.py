"""Shared plumbing for single-document processing stages (normalize, clean, dedupe, extract, classify).

Every stage reads the previous stage's JSON from S3, writes its own JSON envelope with the input checksum and the
service version, and is skipped when a matching output already exists (idempotent retries).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI

from . import aws, utils
from .audit import AuditWriter
from .auth import make_auth_dependency
from .config import Settings
from .exceptions import CleanDocsError, NotFound, ValidationFailure, classify_exception
from .models import DocumentStatus, ExecutionStatus, ProcessRequest, ProcessResponse, TraceContext
from .registry import Registry
from .service import add_executions_endpoint, create_service_app, ttl_cached


@dataclass
class StageDeps:
    settings: Settings
    s3: Any
    registry: Registry
    audit: AuditWriter
    textract: Any = None
    bedrock_agent: Any = None


def stage_key(document_id: str, stage_dir: str, filename: str) -> str:
    return utils.processed_key(document_id, stage_dir, filename)


def read_object(deps: StageDeps, key: str) -> bytes | None:
    try:
        return deps.s3.get_object(Bucket=deps.settings.s3_bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise classify_exception(exc) from exc


def load_document(deps: StageDeps, ctx: TraceContext) -> tuple[dict[str, Any], TraceContext]:
    if not ctx.document_id:
        raise ValidationFailure("context.document_id is required", code="DOCUMENT_ID_REQUIRED")
    doc = deps.registry.get_document(ctx.document_id)
    if doc is None:
        raise NotFound("document is not registered", code="DOCUMENT_NOT_FOUND")
    ctx = ctx.model_copy(update={"ingestion_id": ctx.ingestion_id or doc["ingestion_id"],
                                 "parent_document_id": ctx.parent_document_id or doc.get("parent_document_id")})
    return doc, ctx


def require_input(deps: StageDeps, key: str, what: str) -> tuple[bytes, dict[str, Any]]:
    raw = read_object(deps, key)
    if raw is None:
        raise NotFound(f"{what} not found; run the previous stage first", code="PREVIOUS_STAGE_OUTPUT_MISSING")
    return raw, json.loads(raw)


Compute = Callable[[], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]  # -> (data, summary, metrics)


def execute_stage(
    deps: StageDeps, ctx: TraceContext, *, doc: dict[str, Any], version: str, out_key: str, in_key: str | None,
    input_checksum: str, compute: Compute, stage: str | None, status_on_ok: str | None = None,
    extra_doc_fields: Callable[[dict[str, Any]], dict[str, Any]] | None = None, fail_document: bool = True,
) -> ProcessResponse:
    """Run `compute` once per (input checksum, service version); otherwise return the stored summary."""
    bucket = deps.settings.s3_bucket
    in_uri = utils.s3_uri(bucket, in_key) if in_key else doc.get("s3_uri_original")
    with deps.audit.execution(ctx, input_s3_uri=in_uri, input_checksum=input_checksum) as rec:
        prev_raw = read_object(deps, out_key)
        if prev_raw:
            prev = json.loads(prev_raw)
            if prev.get("input_checksum") == input_checksum and prev.get("service_version") == version:
                rec.skip("output already exists for this input checksum and service version")
                return ProcessResponse(
                    service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                    output_s3_uri=utils.s3_uri(bucket, out_key), output_checksum=utils.sha256_bytes(prev_raw),
                    skipped_reason=rec.skipped_reason, result=prev["summary"],
                )
        try:
            data, summary, metrics = compute()
        except CleanDocsError as err:
            if not err.retryable and fail_document:  # optional branches (graph) must never fail the document
                deps.registry.update_document(ctx.document_id, status=DocumentStatus.FAILED.value, error_code=err.code,
                                              error_message=err.message[:200])
                deps.registry.update_member(doc["ingestion_id"], ctx.document_id,
                                            status=DocumentStatus.FAILED.value, error_code=err.code)
            raise
        envelope = {"document_id": ctx.document_id, "input_checksum": input_checksum, "service_version": version,
                    "produced_at": utils.iso(), "summary": summary, "data": data}
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode()
        deps.s3.put_object(Bucket=bucket, Key=out_key, Body=body, ContentType="application/json")
        checksum = utils.sha256_bytes(body)
        fields: dict[str, Any] = {}
        if stage:
            fields["stage"] = stage
        if status_on_ok:
            fields["status"] = status_on_ok
        if extra_doc_fields:
            fields.update(extra_doc_fields(summary))
        if fields:
            deps.registry.update_document(ctx.document_id, **fields)
        out_uri = utils.s3_uri(bucket, out_key)
        rec.set_output(out_uri, checksum)
        for k, v in metrics.items():
            rec.add_metric(k, v)
        return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
                               output_s3_uri=out_uri, output_checksum=checksum, metrics=metrics, result=summary)


def default_deps(settings: Settings, service_name: str, version: str, with_textract: bool = False,
                 with_bedrock_agent: bool = False) -> StageDeps:
    return StageDeps(
        settings=settings, s3=aws.s3_client(settings), registry=Registry(aws.registry_table(settings)),
        audit=AuditWriter(aws.trail_table(settings), service_name, version, settings.render_instance_id),
        textract=aws.textract_client(settings) if with_textract else None,
        bedrock_agent=aws.bedrock_agent_client(settings) if with_bedrock_agent else None,
    )


def build_stage_app(
    service_name: str, version: str, run: Callable[[StageDeps, ProcessRequest], ProcessResponse],
    settings: Settings | None = None, deps: StageDeps | None = None, with_textract: bool = False,
    with_bedrock_agent: bool = False, extra_routes: Callable[[FastAPI, StageDeps, Callable[..., None]], None] | None = None,
) -> FastAPI:
    settings = settings or Settings(service_name=service_name, service_version=version)
    d = deps or default_deps(settings, service_name, version, with_textract, with_bedrock_agent)

    def s3_ok() -> bool:
        d.s3.head_bucket(Bucket=settings.s3_bucket)
        return True

    def registry_ok() -> bool:
        return d.registry._t.table_status in ("ACTIVE", "UPDATING")  # noqa: SLF001

    app = create_service_app(settings, {"s3": ttl_cached(s3_ok), "registry": ttl_cached(registry_ok)})
    auth = make_auth_dependency(lambda: settings.service_auth_secret.get_secret_value())
    add_executions_endpoint(app, lambda: d.audit._table, auth, service_name)  # noqa: SLF001

    @app.post("/api/v1/process", response_model=ProcessResponse, dependencies=[Depends(auth)])
    def process(req: ProcessRequest) -> ProcessResponse:
        return run(d, req)

    if extra_routes:
        extra_routes(app, d, auth)
    return app
