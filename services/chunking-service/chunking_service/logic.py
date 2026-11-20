"""chunking-service: extraction JSON -> citation-ready chunks (processed/.../chunks/chunks.json, staging only).

Nothing is written to kb-source here: a document only becomes visible to the Knowledge Base once embedding-service
has validated and published all of its chunks.
"""
from __future__ import annotations

from cleandocs_shared import utils
from cleandocs_shared.exceptions import PermanentBusinessFailure
from cleandocs_shared.models import DocumentStage, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, require_input, stage_key

from . import VERSION
from .chunker import chunk_document


def out_key(document_id: str) -> str:
    return stage_key(document_id, "chunks", "chunks.json")


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    s = deps.settings
    in_key = stage_key(ctx.document_id, "extraction", "extraction.json")
    raw, env = require_input(deps, in_key, "extraction output")
    version = f"{VERSION}:{s.chunk_target_chars}/{s.chunk_max_chars}/{s.chunk_overlap_chars}"  # config change re-chunks

    def compute():
        chunks = chunk_document(env["data"]["pages"], target=s.chunk_target_chars, max_chars=s.chunk_max_chars,
                                overlap=s.chunk_overlap_chars)
        if not chunks:
            raise PermanentBusinessFailure("document produced no chunks", code="NO_CHUNKS")
        sizes = [c["char_count"] for c in chunks]
        data = {"document": env["data"]["document"], "chunks": chunks}
        summary = {"chunk_count": len(chunks), "total_chars": sum(sizes), "max_chunk_chars": max(sizes),
                   "avg_chunk_chars": round(sum(sizes) / len(sizes))}
        return data, summary, {"chunks": len(chunks), "avg_chars": summary["avg_chunk_chars"]}

    return execute_stage(deps, ctx, doc=doc, version=version, out_key=out_key(ctx.document_id), in_key=in_key,
                         input_checksum=utils.sha256_bytes(raw), compute=compute, stage=DocumentStage.CHUNKED.value,
                         extra_doc_fields=lambda sm: {"chunk_count": sm["chunk_count"]})
