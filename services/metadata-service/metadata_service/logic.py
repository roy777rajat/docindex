"""metadata-service: deterministic title/type/language/dates/keywords. The Bedrock fallback is not enabled yet."""
from __future__ import annotations

from cleandocs_shared import utils
from cleandocs_shared.models import DocumentStage, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, require_input, stage_key

from . import VERSION, rules


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    in_key = stage_key(ctx.document_id, "extraction", "extraction.json")
    raw, env = require_input(deps, in_key, "extraction output")
    in_checksum = utils.sha256_bytes(raw)

    def compute():
        d = env["data"]
        pages = d["pages"]
        text = "\n".join(b["text"] for p in pages for b in p["blocks"])
        kind = d["document"]["kind"]
        doc_type, conf, hits = rules.classify(text, kind)
        meta = {
            "title": rules.pick_title(pages, doc["original_filename"]), "doc_type": doc_type,
            "doc_type_confidence": conf, "doc_type_signals": hits, "language": rules.language_hint(text),
            "dates_mentioned": rules.dates(text), "keywords": rules.keywords(text),
            "page_count": len(pages), "word_count": len(text.split()), "char_count": len(text),
            "source": {"filename": doc["original_filename"], "kind": kind,
                       "parent_document_id": doc.get("parent_document_id"),
                       "archive_entry_path": doc.get("archive_entry_path")},
            "llm_used": False,
            "llm_skipped_reason": "deterministic rules only in this release" if conf < 0.5 else None,
        }
        return meta, meta, {"doc_type": doc_type, "word_count": meta["word_count"]}

    return execute_stage(
        deps, ctx, doc=doc, version=VERSION, out_key=stage_key(ctx.document_id, "metadata", "metadata.json"),
        in_key=in_key, input_checksum=in_checksum, compute=compute, stage=DocumentStage.CLASSIFIED.value,
        extra_doc_fields=lambda s: {"title": s["title"], "doc_type": s["doc_type"], "language": s["language"],
                                    "word_count": s["word_count"]},
    )
