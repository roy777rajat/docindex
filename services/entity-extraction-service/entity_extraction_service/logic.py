"""entity-extraction-service (optional, non-blocking): regex entity candidates with evidence -> graph-candidates/.

A failure here never fails the document; the graph branch is optional and its status is tracked separately.
"""
from __future__ import annotations

import hashlib
import re

from cleandocs_shared import utils
from cleandocs_shared.graph_models import Entity, EvidenceRef
from cleandocs_shared.models import ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, read_object, require_input, stage_key

from . import VERSION
from .patterns import RULES, trim_leading_noise

MAX_ENTITIES = 500
MAX_EVIDENCE_PER_ENTITY = 5


def out_key(document_id: str) -> str:
    return stage_key(document_id, "graph-candidates", "entities.json")


def _normalize(etype: str, text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    if etype == "MONEY":
        return re.sub(r"[\s,]", "", t).upper()
    if etype in ("EMAIL", "IDENTIFIER", "ORGANIZATION", "PERSON"):
        return t.lower().rstrip(".")
    return t


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    in_key = stage_key(ctx.document_id, "extraction", "extraction.json")
    raw, env = require_input(deps, in_key, "extraction output")
    chunks_raw = read_object(deps, stage_key(ctx.document_id, "chunks", "chunks.json"))
    in_checksum = utils.sha256_bytes(raw + (chunks_raw or b""))

    block_to_chunks: dict[str, list[str]] = {}
    if chunks_raw:
        import json

        for c in json.loads(chunks_raw)["data"]["chunks"]:
            for b in c["block_ids"]:
                block_to_chunks.setdefault(b, []).append(c["chunk_id"])

    def compute():
        found: dict[tuple[str, str], Entity] = {}
        for page in env["data"]["pages"]:
            for block in page["blocks"]:
                text = block["text"]
                for etype, rx, grp, conf in RULES:
                    for m in rx.finditer(text):
                        value = m.group(grp).strip()
                        if etype == "IDENTIFIER" and not any(ch.isdigit() for ch in value):
                            continue
                        s, e = m.span(grp)
                        if etype == "ORGANIZATION":
                            value, cut = trim_leading_noise(value)
                            s += cut
                        norm = _normalize(etype, value)
                        ev = EvidenceRef(
                            document_id=ctx.document_id, chunk_ids=block_to_chunks.get(block["id"], []), block_id=block["id"],
                            page=page.get("page"), sheet=page.get("sheet"), section=(block.get("section") or None) and block["section"][:120],
                            char_start=s, char_end=e, snippet=re.sub(r"\s+", " ", text[max(0, s - 30): e + 30])[:160],
                            archive_entry_path=doc.get("archive_entry_path"),
                        )
                        key = (etype, norm)
                        if key in found:
                            if len(found[key].evidence) < MAX_EVIDENCE_PER_ENTITY:
                                found[key].evidence.append(ev)
                        elif len(found) < MAX_ENTITIES:
                            eid = "ent_" + hashlib.sha256(f"{ctx.document_id}|{etype}|{norm}".encode()).hexdigest()[:16]
                            found[key] = Entity(entity_id=eid, type=etype, text=value[:200], normalized=norm[:200],
                                                confidence=conf, method="regex", evidence=[ev])
        entities = [e.model_dump(mode="json") for e in sorted(found.values(), key=lambda x: (x.type.value, x.normalized))]
        by_type: dict[str, int] = {}
        for e in entities:
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        data = {"document_id": ctx.document_id, "method": "regex", "entities": entities}
        summary = {"entity_count": len(entities), "by_type": by_type, "truncated": len(found) >= MAX_ENTITIES}
        return data, summary, {"entities": len(entities)}

    return execute_stage(
        deps, ctx, doc=doc, version=VERSION, out_key=out_key(ctx.document_id), in_key=in_key, input_checksum=in_checksum,
        compute=compute, stage=None, fail_document=False,
        extra_doc_fields=lambda s: {"entity_candidate_count": s["entity_count"]},
    )
