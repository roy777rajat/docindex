"""Validation workflow for one document: fetch, verify checksum, run content checks, record the verdict."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from cleandocs_shared import utils
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import CleanDocsError, NotFound, ValidationFailure, classify_exception
from cleandocs_shared.logging_utils import get_logger
from cleandocs_shared.models import DocumentStage, DocumentStatus, ExecutionStatus, ProcessResponse, TraceContext
from cleandocs_shared.registry import Registry

from . import VERSION
from .checks import validate_content

log = get_logger(__name__)
_LATER_STAGES = {s.value for s in DocumentStage if s not in (DocumentStage.UPLOADED,)}


@dataclass
class ValidationDeps:
    settings: Settings
    s3: Any
    registry: Registry
    audit: AuditWriter


def result_key(document_id: str) -> str:
    return utils.processed_key(document_id, "metadata", "validation.json")


def _load_previous(deps: ValidationDeps, document_id: str, checksum: str) -> dict[str, Any] | None:
    """A previous successful result is reusable only for the same input checksum and service version."""
    try:
        obj = deps.s3.get_object(Bucket=deps.settings.s3_bucket, Key=result_key(document_id))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise classify_exception(exc) from exc
    prev = json.loads(obj["Body"].read())
    if prev.get("input_checksum") == checksum and prev.get("service_version") == VERSION and prev.get("valid") is True:
        return prev
    return None


def run_validation(deps: ValidationDeps, ctx: TraceContext, input_s3_uri: str | None, declared_mime: str | None = None) -> ProcessResponse:
    settings = deps.settings
    if not ctx.document_id:
        raise ValidationFailure("context.document_id is required", code="DOCUMENT_ID_REQUIRED")
    doc = deps.registry.get_document(ctx.document_id)
    if doc is None:
        raise NotFound("document is not registered", code="DOCUMENT_NOT_FOUND")

    uri = input_s3_uri or doc["s3_uri_original"]
    bucket, key = utils.parse_s3_uri(uri)
    if bucket != settings.s3_bucket:
        raise ValidationFailure("input is outside the CleanDocs bucket", code="FOREIGN_BUCKET")
    ctx = ctx.model_copy(update={"ingestion_id": ctx.ingestion_id or doc["ingestion_id"],
                                 "parent_document_id": ctx.parent_document_id or doc.get("parent_document_id")})
    expected = doc["checksum_sha256"]

    with deps.audit.execution(ctx, input_s3_uri=uri, input_checksum=expected) as rec:
        previous = _load_previous(deps, ctx.document_id, expected)
        if previous and doc["stage"] in _LATER_STAGES:
            rec.skip("validation already completed for this checksum and service version")
            return ProcessResponse(
                service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                output_s3_uri=utils.s3_uri(bucket, result_key(ctx.document_id)),
                skipped_reason=rec.skipped_reason, result=previous["result"],
            )

        try:
            with tempfile.TemporaryFile() as tmp:
                try:
                    deps.s3.download_fileobj(bucket, key, tmp)
                except ClientError as exc:
                    raise classify_exception(exc) from exc
                tmp.seek(0)
                actual, size = utils.sha256_stream(tmp)
                if actual != expected:
                    raise ValidationFailure("stored bytes do not match the registered checksum", code="CHECKSUM_MISMATCH")
                result = validate_content(
                    tmp, filename=doc["original_filename"],
                    declared_mime=declared_mime or doc.get("content_type_declared"), size=size, settings=settings,
                )
        except CleanDocsError as err:
            if not err.retryable:  # business rejection: record on the document and its manifest entry
                deps.registry.update_document(
                    ctx.document_id, status=DocumentStatus.REJECTED.value, stage=DocumentStage.UPLOADED.value,
                    rejection_code=err.code, rejection_reason=err.message[:200],
                )
                deps.registry.update_member(doc["ingestion_id"], ctx.document_id,
                                            status=DocumentStatus.REJECTED.value, rejection_code=err.code)
            raise

        payload = {
            "document_id": ctx.document_id, "valid": True, "input_checksum": expected,
            "service_version": VERSION, "validated_at": utils.iso(), "result": result,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        out_key = result_key(ctx.document_id)
        deps.s3.put_object(Bucket=bucket, Key=out_key, Body=body, ContentType="application/json")
        out_uri = utils.s3_uri(bucket, out_key)

        deps.registry.update_document(
            ctx.document_id, stage=DocumentStage.VALIDATED.value, file_kind=result["file_kind"],
            detected_mime=result["detected_mime"], is_archive=result["is_archive"],
            page_count=result.get("pages"),
        )
        if result["is_archive"]:  # containers are expanded by archive-service, not processed as content
            deps.registry.update_member(doc["ingestion_id"], ctx.document_id, role="ARCHIVE")

        rec.set_output(out_uri, utils.sha256_bytes(body))
        rec.add_metric("size_bytes", size)
        rec.add_metric("file_kind", result["file_kind"])
        return ProcessResponse(
            service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
            output_s3_uri=out_uri, output_checksum=utils.sha256_bytes(body),
            metrics={"size_bytes": size}, result=result,
        )
