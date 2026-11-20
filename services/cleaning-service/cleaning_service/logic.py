"""cleaning-service: safe text clean-up on normalized blocks, with a frozen-token guard for numbers/dates/currency."""
from __future__ import annotations

from collections import Counter
from typing import Any

from cleandocs_shared import utils
from cleandocs_shared.exceptions import PermanentBusinessFailure
from cleandocs_shared.models import DocumentStage, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, require_input, stage_key
from cleandocs_shared.textclean import clean_text, protected_tokens

from . import VERSION

IN_NAME = ("normalized", "normalized.json")


def out_key(document_id: str) -> str:
    return stage_key(document_id, "cleaned", "cleaned.json")


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    in_key = stage_key(ctx.document_id, *IN_NAME)
    raw, env = require_input(deps, in_key, "normalized output")
    in_checksum = utils.sha256_bytes(raw)

    def compute():
        data: dict[str, Any] = env["data"]
        before: Counter = Counter()
        after: Counter = Counter()
        removed = changed = 0
        for page in data["pages"]:
            kept = []
            for b in page["blocks"]:
                before.update(protected_tokens(b["text"]))
                new = clean_text(b["text"], join_lines=b["type"] in ("paragraph", "heading"))
                after.update(protected_tokens(new))
                if not new:
                    removed += 1
                    continue
                if new != b["text"]:
                    changed += 1
                kept.append({**b, "text": new})
            page["blocks"] = kept
        if before != after:
            diff = (before - after) + (after - before)
            raise PermanentBusinessFailure("cleaning altered protected numeric tokens", code="PROTECTED_TOKENS_CHANGED",
                                           details={"example_count": len(diff)})
        data["cleaning"] = {"service_version": VERSION, "blocks_changed": changed, "blocks_removed": removed,
                            "protected_token_count": sum(before.values())}
        chars = sum(len(b["text"]) for p in data["pages"] for b in p["blocks"])
        summary = {**env["summary"], "chars": chars, "blocks_changed": changed, "blocks_removed": removed,
                   "protected_tokens_verified": sum(before.values())}
        return data, summary, {"blocks_changed": changed, "blocks_removed": removed, "chars": chars}

    return execute_stage(deps, ctx, doc=doc, version=VERSION, out_key=out_key(ctx.document_id), in_key=in_key,
                         input_checksum=in_checksum, compute=compute, stage=DocumentStage.CLEANED.value)
