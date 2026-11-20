"""Upload business logic: hash, store the untouched original, register, hand off to the workflow.

The upload service never validates content or processes the document; it only guarantees that the
exact bytes received are durably stored, registered, and that the ingestion workflow is started.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, BinaryIO

from botocore.exceptions import ClientError

from cleandocs_shared import utils
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import ValidationFailure, classify_exception
from cleandocs_shared.filetypes import extension_of
from cleandocs_shared.logging_utils import get_logger
from cleandocs_shared.models import TraceContext
from cleandocs_shared.registry import Registry

log = get_logger(__name__)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_BLOCK = 1024 * 1024


@dataclass
class UploadDeps:
    settings: Settings
    s3: Any
    registry: Registry
    audit: AuditWriter
    sfn: Any


def hash_stream(fileobj: BinaryIO, max_bytes: int) -> tuple[str, int]:
    """SHA-256 and size of an uploaded stream, enforcing the size limit while reading (not after)."""
    h = hashlib.sha256()
    size = 0
    while True:
        block = fileobj.read(_BLOCK)
        if not block:
            break
        size += len(block)
        if size > max_bytes:
            raise ValidationFailure(
                f"file exceeds the {max_bytes // (1024 * 1024)} MB limit", code="FILE_TOO_LARGE",
                details={"max_bytes": max_bytes},
            )
        h.update(block)
    if size == 0:
        raise ValidationFailure("uploaded file is empty", code="EMPTY_FILE")
    fileobj.seek(0)
    return h.hexdigest(), size


def validate_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not _IDEMPOTENCY_KEY.match(key):
        raise ValidationFailure("Idempotency-Key must be 8-128 chars of A-Z a-z 0-9 . _ -", code="BAD_IDEMPOTENCY_KEY")
    return key


def _start_workflow(deps: UploadDeps, ingestion_id: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    arn = deps.settings.step_functions_ingestion_arn
    if not arn:
        return False, None
    try:
        resp = deps.sfn.start_execution(stateMachineArn=arn, name=ingestion_id, input=json.dumps(payload))
        return True, resp["executionArn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
            return True, None  # same ingestion id already started: idempotent
        log.error("workflow start failed", fields={"error": classify_exception(exc).code})
        return False, None


def handle_upload(
    deps: UploadDeps, *, fileobj: BinaryIO, filename: str, content_type: str | None,
    idempotency_key: str | None, label: str | None, trace_id: str,
) -> dict[str, Any]:
    settings = deps.settings
    key = validate_idempotency_key(idempotency_key)
    if key:
        previous = deps.registry.get_idempotent_response(key)
        if previous:
            return {**previous, "idempotent_replay": True}

    if not filename or not filename.strip():
        raise ValidationFailure("filename is required", code="FILENAME_REQUIRED")
    clean_name = utils.safe_filename(filename)
    ext = extension_of(clean_name)

    checksum, size = hash_stream(fileobj, settings.max_file_size_bytes)
    ingestion_id = utils.new_ingestion_id()
    document_id = utils.new_document_id()
    ctx = TraceContext(ingestion_id=ingestion_id, document_id=document_id, trace_id=trace_id)
    key_s3 = utils.raw_original_key(document_id, clean_name)
    s3_uri = utils.s3_uri(settings.s3_bucket, key_s3)

    with deps.audit.execution(ctx, input_checksum=checksum) as rec:
        try:
            deps.s3.put_object(
                Bucket=settings.s3_bucket, Key=key_s3, Body=fileobj, ContentLength=size,
                ContentType="application/octet-stream",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode(),  # S3 verifies integrity
                IfNoneMatch="*",  # originals are never overwritten
                Metadata={"document-id": document_id, "sha256": checksum},
            )
        except ClientError as exc:
            raise classify_exception(exc) from exc

        deps.registry.create_ingestion(ingestion_id=ingestion_id, trace_id=trace_id, label=label)
        deps.registry.create_document(
            document_id=document_id, ingestion_id=ingestion_id, original_filename=clean_name, extension=ext,
            size_bytes=size, checksum_sha256=checksum, s3_uri_original=s3_uri, content_type_declared=content_type,
        )
        deps.registry.add_member(ingestion_id=ingestion_id, document_id=document_id, role="FILE",
                                 s3_uri=s3_uri, checksum_sha256=checksum, original_filename=clean_name)

        started, execution_arn = _start_workflow(deps, ingestion_id, {
            "ingestion_id": ingestion_id, "document_id": document_id, "trace_id": trace_id,
            "s3_uri": s3_uri, "checksum_sha256": checksum, "original_filename": clean_name,
        })
        deps.registry.update_ingestion(
            ingestion_id, workflow_status="STARTED" if started else "NOT_STARTED", workflow_execution_arn=execution_arn,
        )
        rec.set_output(s3_uri, checksum)
        rec.add_metric("size_bytes", size)
        rec.add_metric("workflow_started", int(started))

    response = {
        "ingestion_id": ingestion_id, "document_id": document_id, "filename": clean_name,
        "size_bytes": size, "checksum_sha256": checksum, "s3_uri": s3_uri,
        "workflow_started": started, "workflow_execution_arn": execution_arn, "trace_id": trace_id,
        "idempotent_replay": False,
    }
    if key:
        deps.registry.store_idempotent_response(key, response)
    return response
