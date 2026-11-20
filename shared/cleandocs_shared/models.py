"""Common models shared by every service (data contracts only, no business logic)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExecutionStatus(str, Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RETRIED = "RETRIED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class DocumentStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class GraphStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DocumentStage(str, Enum):
    UPLOADED = "UPLOADED"
    VALIDATED = "VALIDATED"
    EXTRACTED_ARCHIVE = "EXTRACTED_ARCHIVE"
    NORMALIZED = "NORMALIZED"
    CLEANED = "CLEANED"
    DEDUP_CHECKED = "DEDUP_CHECKED"
    EXTRACTED = "EXTRACTED"
    CLASSIFIED = "CLASSIFIED"
    CHUNKED = "CHUNKED"
    EMBEDDING_PREPARED = "EMBEDDING_PREPARED"
    INDEX_READY = "INDEX_READY"
    INDEXED = "INDEXED"
    RECONCILED = "RECONCILED"


class DuplicatePolicy(str, Enum):
    SKIP = "SKIP"
    LINK = "LINK"
    REPROCESS = "REPROCESS"


class TraceContext(BaseModel):
    """Identifiers carried on every request, log line and audit record."""

    model_config = ConfigDict(extra="forbid")

    ingestion_id: str | None = None
    document_id: str | None = None
    parent_document_id: str | None = None
    workflow_execution_id: str | None = None
    trace_id: str | None = None


class FileMessage(BaseModel):
    """SQS message: one independent file-level processing unit. Never carries content."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    ingestion_id: str
    parent_document_id: str | None = None
    s3_uri: str
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt: int = Field(default=1, ge=1)
    trace_id: str


class ProcessRequest(BaseModel):
    """Body for POST /api/v1/process on every file-level service."""

    model_config = ConfigDict(extra="forbid")

    context: TraceContext
    input_s3_uri: str | None = None
    input_checksum: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_execution_id: str
    status: ExecutionStatus
    output_s3_uri: str | None = None
    output_checksum: str | None = None
    skipped_reason: str | None = None
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    failure_class: str
    retryable: bool
    correlation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ServiceInfo(BaseModel):
    service_name: str
    service_version: str
    shared_version: str
    api_version: str = "v1"
    region: str
    render_instance_id: str | None = None
