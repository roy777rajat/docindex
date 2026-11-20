"""Structured JSON logging with correlation identifiers and content redaction."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

CONTEXT_FIELDS = (
    "ingestion_id",
    "document_id",
    "parent_document_id",
    "service_name",
    "service_execution_id",
    "workflow_execution_id",
    "trace_id",
    "correlation_id",
)

# Keys whose values may carry document content or secrets; never logged.
REDACTED_KEYS = frozenset(
    {
        "text", "content", "body", "extracted_text", "raw_text", "chunk_text",
        "password", "secret", "authorization", "api_key", "token", "service_auth_secret",
    }
)

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context", default={})


def bind_context(**fields: Any) -> None:
    """Add identifiers to every subsequent log line in this execution context."""
    current = dict(_context.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    _context.set(current)


def clear_context() -> None:
    _context.set({})


def get_context() -> dict[str, Any]:
    return dict(_context.get())


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if str(k).lower() in REDACTED_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str = "unknown-service", service_version: str = "0.0.0") -> None:
        super().__init__()
        self._service_name = service_name
        self._service_version = service_version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service_name": self._service_name,
            "service_version": self._service_version,
        }
        payload.update(get_context())
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            # exception type and location only: messages can embed document content
            etype = record.exc_info[0]
            payload["exception_type"] = etype.__name__ if etype else None
            payload["exception_trace"] = self.formatException(record.exc_info).splitlines()[-1][:300]
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(service_name: str, service_version: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name, service_version))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # keep AWS SDK chatter (and its payloads) out of logs
    for noisy in ("boto3", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.LoggerAdapter:
    class _Adapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            fields = kwargs.pop("fields", None)
            if fields is not None:
                kwargs.setdefault("extra", {})["fields"] = fields
            return msg, kwargs

    return _Adapter(logging.getLogger(name), {})
