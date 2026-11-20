"""embedding-service (embedding-prep only): validate chunks and publish them to kb-source for Bedrock to embed.

Bedrock Knowledge Bases create the embeddings (Titan Text Embeddings V2); this service never calls an embedding model.
One kb-source object is one chunk (data source chunking NONE), with a `.metadata.json` sidecar carrying the citation
attributes. Chunk objects left over from an earlier version of the document are removed so they cannot be indexed.
"""
from __future__ import annotations

import json
import math
from typing import Any

from cleandocs_shared import utils
from cleandocs_shared.exceptions import PermanentBusinessFailure
from cleandocs_shared.models import DocumentStage, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, require_input, stage_key

from . import VERSION

MAX_EST_TOKENS = 7000       # Titan v2 accepts 8,192 tokens; chars/3 is a conservative estimate
SIDECAR_BUDGET_BYTES = 1500  # S3 Vectors allows 2 KB of filterable metadata per vector; keep headroom for Bedrock's keys


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / 3)


def build_attributes(doc: dict[str, Any], meta: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    """Citation attributes. Absent locators are omitted, never invented. Everything else is resolvable via the
    chunk manifest and registry using document_id + chunk_id."""
    a: dict[str, Any] = {
        "document_id": doc["document_id"], "chunk_id": chunk["chunk_id"], "chunk_index": chunk["index"],
        "filename": doc["original_filename"][:120], "ingestion_id": doc["ingestion_id"],
        "doc_type": meta.get("doc_type", "general"),
    }
    optional = {"parent_document_id": doc.get("parent_document_id"),
                "archive_entry_path": (doc.get("archive_entry_path") or "")[:200] or None,
                "page_start": chunk.get("page_start"), "page_end": chunk.get("page_end"),
                "sheet": (chunk.get("sheet") or "")[:60] or None, "section": (chunk.get("section") or "")[:120] or None}
    a.update({k: v for k, v in optional.items() if v is not None})
    for shrink in ("section", "archive_entry_path", "filename"):  # last resort if names are unusually long
        while len(json.dumps({"metadataAttributes": a}).encode()) > SIDECAR_BUDGET_BYTES and len(str(a.get(shrink, ""))) > 20:
            a[shrink] = str(a[shrink])[: len(str(a[shrink])) // 2]
    if len(json.dumps({"metadataAttributes": a}).encode()) > SIDECAR_BUDGET_BYTES:
        raise PermanentBusinessFailure("chunk metadata exceeds the sidecar budget", code="METADATA_TOO_LARGE")
    return a


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    bucket = deps.settings.s3_bucket
    in_key = stage_key(ctx.document_id, "chunks", "chunks.json")
    raw, env = require_input(deps, in_key, "chunking output")
    meta_raw, meta_env = require_input(deps, stage_key(ctx.document_id, "metadata", "metadata.json"), "metadata output")
    in_checksum = utils.sha256_bytes(raw + meta_raw)

    def compute():
        chunks = env["data"]["chunks"]
        entries, keep = [], set()
        for c in chunks:
            text = c["text"]
            if not text.strip():
                raise PermanentBusinessFailure("empty chunk", code="EMPTY_CHUNK")
            if est_tokens(text) > MAX_EST_TOKENS:
                raise PermanentBusinessFailure("chunk exceeds the embedding token limit", code="CHUNK_TOO_LARGE")
            attrs = build_attributes(doc, meta_env["data"], c)
            key, mkey = utils.kb_chunk_key(ctx.document_id, c["chunk_id"]), utils.kb_chunk_metadata_key(ctx.document_id, c["chunk_id"])
            deps.s3.put_object(Bucket=bucket, Key=key, Body=text.encode(), ContentType="text/plain; charset=utf-8")
            deps.s3.put_object(Bucket=bucket, Key=mkey, Body=json.dumps({"metadataAttributes": attrs}).encode(),
                               ContentType="application/json")
            keep.update({key, mkey})
            entries.append({
                "chunk_id": c["chunk_id"], "index": c["index"], "text_sha256": utils.sha256_bytes(text.encode()),
                "s3_uri": utils.s3_uri(bucket, key), "metadata_s3_uri": utils.s3_uri(bucket, mkey),
                "page_start": c.get("page_start"), "page_end": c.get("page_end"), "sheet": c.get("sheet"),
                "section": c.get("section"), "est_tokens": est_tokens(text),
            })
        removed = _remove_stale(deps, ctx.document_id, keep)
        data = {"document_id": ctx.document_id, "chunk_count": len(entries), "chunks": entries}
        summary = {"chunk_count": len(entries), "stale_objects_removed": removed, "kb_prefix": f"kb-source/{ctx.document_id}/",
                   "max_est_tokens": max(e["est_tokens"] for e in entries)}
        return data, summary, {"chunks": len(entries), "stale_removed": removed}

    return execute_stage(
        deps, ctx, doc=doc, version=VERSION, out_key=stage_key(ctx.document_id, "manifests", "chunks.json"), in_key=in_key,
        input_checksum=in_checksum, compute=compute, stage=DocumentStage.INDEX_READY.value,
        extra_doc_fields=lambda s: {"indexing_state": "PENDING", "chunk_count": s["chunk_count"]},
    )


def _remove_stale(deps: StageDeps, document_id: str, keep: set[str]) -> int:
    prefix, removed, token = f"kb-source/{document_id}/", 0, None
    while True:
        kw = {"Bucket": deps.settings.s3_bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = deps.s3.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            if o["Key"] not in keep:
                deps.s3.delete_object(Bucket=deps.settings.s3_bucket, Key=o["Key"])
                removed += 1
        if not resp.get("IsTruncated"):
            return removed
        token = resp["NextContinuationToken"]
