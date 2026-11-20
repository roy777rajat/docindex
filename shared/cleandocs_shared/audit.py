"""Immutable audit trail writer for the `document-ingest-trail` table.

Every service invocation writes a STARTED record and later one terminal record
(COMPLETED / FAILED / SKIPPED / NOT_IMPLEMENTED). Rows are written with
attribute_not_exists conditions and are never updated. A retry is a new execution
with a new service_execution_id plus a RETRIED row that points at the previous one.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from botocore.exceptions import ClientError

from .exceptions import CleanDocsError, classify_exception
from .logging_utils import bind_context, get_logger
from .models import ExecutionStatus, TraceContext
from .utils import doc_pk, ingestion_pk, iso, new_execution_id, to_dynamo as _to_dynamo, utcnow

log = get_logger(__name__)

_MAX_ERROR_MESSAGE = 300

# Lifecycle rank keeps rows of one execution in order when queried (alphabetical event
# names would sort COMPLETED before STARTED).
_RANK = {
    ExecutionStatus.RETRIED: 0,
    ExecutionStatus.STARTED: 1,
    ExecutionStatus.COMPLETED: 2,
    ExecutionStatus.FAILED: 2,
    ExecutionStatus.SKIPPED: 2,
    ExecutionStatus.NOT_IMPLEMENTED: 2,
}


def _sk(started_at: str, execution_id: str, status: ExecutionStatus) -> str:
    return f"EXEC#{started_at}#{execution_id}#{_RANK[status]}#{status.value}"


class AuditWriter:
    def __init__(
        self,
        table: Any,  # boto3 DynamoDB Table resource
        service_name: str,
        service_version: str,
        render_instance_id: str | None = None,
    ) -> None:
        self._table = table
        self.service_name = service_name
        self.service_version = service_version
        self.render_instance_id = render_instance_id

    def _put(self, item: dict[str, Any]) -> None:
        item = {k: _to_dynamo(v) for k, v in item.items() if v is not None}
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Immutability held: refusing to overwrite is the correct outcome.
                raise CleanDocsError("audit record already exists", code="AUDIT_IMMUTABLE") from exc
            raise

    def _base(self, ctx: TraceContext, execution_id: str, started_at: str, attempt: int) -> dict[str, Any]:
        pk = doc_pk(ctx.document_id) if ctx.document_id else ingestion_pk(ctx.ingestion_id or "unknown")
        return {
            "PK": pk,
            "document_id": ctx.document_id,
            "ingestion_id": ctx.ingestion_id,
            "parent_document_id": ctx.parent_document_id,
            "service_name": self.service_name,
            "service_execution_id": execution_id,
            "workflow_execution_id": ctx.workflow_execution_id,
            "trace_id": ctx.trace_id,
            "started_at": started_at,
            "attempt_number": attempt,
            "service_version": self.service_version,
            "render_instance_id": self.render_instance_id,
        }

    @contextmanager
    def execution(
        self,
        ctx: TraceContext,
        *,
        input_s3_uri: str | None = None,
        input_checksum: str | None = None,
        attempt: int = 1,
        retry_of: str | None = None,
        retry_reason: str | None = None,
    ) -> Iterator["ExecutionRecorder"]:
        execution_id = new_execution_id(self.service_name)
        started_at = iso()
        bind_context(
            ingestion_id=ctx.ingestion_id, document_id=ctx.document_id,
            parent_document_id=ctx.parent_document_id, service_name=self.service_name,
            service_execution_id=execution_id, workflow_execution_id=ctx.workflow_execution_id,
            trace_id=ctx.trace_id,
        )
        base = self._base(ctx, execution_id, started_at, attempt)
        base.update(input_s3_uri=input_s3_uri, input_checksum=input_checksum)

        if retry_of:
            self._put({**base, "SK": _sk(started_at, execution_id, ExecutionStatus.RETRIED),
                       "status": ExecutionStatus.RETRIED.value, "retry_of": retry_of,
                       "retry_reason": retry_reason, "created_at": iso()})
        self._put({**base, "SK": _sk(started_at, execution_id, ExecutionStatus.STARTED),
                   "status": ExecutionStatus.STARTED.value, "created_at": iso()})

        rec = ExecutionRecorder(execution_id)
        t0 = time.perf_counter()
        try:
            yield rec
        except Exception as exc:  # noqa: BLE001 - classify then re-raise
            err = classify_exception(exc)
            self._terminal(base, started_at, execution_id, t0, ExecutionStatus.FAILED, rec, err)
            raise err if err is not exc else exc
        else:
            status = rec.status or ExecutionStatus.COMPLETED
            self._terminal(base, started_at, execution_id, t0, status, rec, None)

    def _terminal(self, base, started_at, execution_id, t0, status, rec, err: CleanDocsError | None) -> None:
        item = {
            **base,
            "SK": _sk(started_at, execution_id, status),
            "status": status.value,
            "completed_at": iso(utcnow()),
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "output_s3_uri": rec.output_s3_uri,
            "output_checksum": rec.output_checksum,
            "metrics": rec.metrics or None,
            "created_at": iso(),
        }
        if status is ExecutionStatus.SKIPPED:
            item["retry_reason"] = rec.skipped_reason
        if err is not None:
            item.update(
                error_code=err.code,
                error_class=err.failure_class.value,
                retryable=err.retryable,
                # message is generic by construction (see classify_exception); truncated as a safeguard
                error_message=err.message[:_MAX_ERROR_MESSAGE],
            )
        self._put(item)
        log.info("service execution finished", fields={"status": status.value, "duration_ms": item["duration_ms"]})


class ExecutionRecorder:
    """Handed to the service body to declare outputs, metrics and non-error outcomes."""

    def __init__(self, execution_id: str) -> None:
        self.service_execution_id = execution_id
        self.status: ExecutionStatus | None = None
        self.output_s3_uri: str | None = None
        self.output_checksum: str | None = None
        self.skipped_reason: str | None = None
        self.metrics: dict[str, Any] = {}

    def set_output(self, s3_uri: str, checksum: str | None = None) -> None:
        self.output_s3_uri = s3_uri
        self.output_checksum = checksum

    def add_metric(self, name: str, value: float | int | str) -> None:
        self.metrics[name] = value

    def skip(self, reason: str) -> None:
        self.status = ExecutionStatus.SKIPPED
        self.skipped_reason = reason

    def not_implemented(self) -> None:
        self.status = ExecutionStatus.NOT_IMPLEMENTED
