"""Small pure helpers: ids, hashing, S3 key builders, filename safety."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import BinaryIO, Iterable
from urllib.parse import urlparse

_CHUNK = 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_ingestion_id() -> str:
    return f"ing_{uuid.uuid4().hex}"


def new_document_id() -> str:
    return f"doc_{uuid.uuid4().hex}"


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_execution_id(service_name: str, now: datetime | None = None) -> str:
    """`<service>_<YYYYMMDDTHHMMSSZ>_<6 hex>`, e.g. validation-service_20261120T151515Z_92eab4."""
    ts = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{service_name}_{ts}_{uuid.uuid4().hex[:6]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO | Iterable[bytes]) -> tuple[str, int]:
    """Hash a file object or iterable of byte chunks without loading it into memory. Returns (hex, size)."""
    h = hashlib.sha256()
    size = 0
    if hasattr(stream, "read"):
        while True:
            block = stream.read(_CHUNK)  # type: ignore[union-attr]
            if not block:
                break
            h.update(block)
            size += len(block)
    else:
        for block in stream:  # type: ignore[assignment]
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, max_len: int = 120) -> str:
    """Reduce a client-supplied filename to a safe S3/key-friendly basename (no paths, no control chars)."""
    base = name.replace("\\", "/").split("/")[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    base = _SAFE_NAME.sub("_", base).strip("._") or "file"
    if len(base) > max_len:
        stem, dot, ext = base.rpartition(".")
        ext = ext[:10]
        base = (stem[: max_len - len(ext) - 1] + "." + ext) if dot else base[:max_len]
    return base


# ---- S3 layout (single bucket, see docs/phase0-architecture.md section 5) ----

def raw_original_key(document_id: str, filename: str) -> str:
    return f"raw/documents/{document_id}/original/{safe_filename(filename)}"


def processed_key(document_id: str, stage: str, filename: str) -> str:
    """stage examples: normalized, cleaned, extraction/raw, extraction/normalized, metadata, manifests."""
    return f"processed/documents/{document_id}/{stage.strip('/')}/{filename}"


def kb_chunk_key(document_id: str, chunk_id: str) -> str:
    return f"kb-source/{document_id}/{chunk_id}.txt"


def kb_chunk_metadata_key(document_id: str, chunk_id: str) -> str:
    return kb_chunk_key(document_id, chunk_id) + ".metadata.json"


def tmp_key(execution_id: str, filename: str) -> str:
    return f"tmp/{execution_id}/{filename}"


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    p = urlparse(uri)
    if p.scheme != "s3" or not p.netloc or not p.path.lstrip("/"):
        raise ValueError("not a valid s3 uri")
    return p.netloc, p.path.lstrip("/")


# ---- DynamoDB key builders (registry table and audit trail) ----

def doc_pk(document_id: str) -> str:
    return f"DOCUMENT#{document_id}"


def ingestion_pk(ingestion_id: str) -> str:
    return f"INGESTION#{ingestion_id}"


def registry_doc_pk(document_id: str) -> str:
    return f"DOC#{document_id}"


def registry_ingest_pk(ingestion_id: str) -> str:
    return f"INGEST#{ingestion_id}"


def checksum_pk(sha256: str) -> str:
    return f"CHECKSUM#{sha256}"


def gate_pk(scope: str = "GLOBAL") -> str:
    return f"GATE#{scope}"


def budget_pk(year_month: str) -> str:
    return f"BUDGET#{year_month}"


# ---- DynamoDB value conversion (boto3 resource API rejects float, returns Decimal) ----

def to_dynamo(value):
    from decimal import Decimal

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value
