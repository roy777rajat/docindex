"""duplicate-service: exact SHA-256 duplicate detection through a conditional checksum claim. No semantic matching."""
from __future__ import annotations

from cleandocs_shared.exceptions import ValidationFailure
from cleandocs_shared.models import DocumentStage, DocumentStatus, DuplicatePolicy, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, stage_key

from . import VERSION


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    try:
        policy = DuplicatePolicy(str(req.parameters.get("duplicate_policy", "SKIP")).upper())
    except ValueError as exc:
        raise ValidationFailure("duplicate_policy must be SKIP, LINK or REPROCESS", code="POLICY_INVALID") from exc
    checksum = doc["checksum_sha256"]

    def compute():
        canonical = deps.registry.claim_checksum(checksum, doc["document_id"])
        is_dup = canonical != doc["document_id"]
        skip = is_dup and policy in (DuplicatePolicy.SKIP, DuplicatePolicy.LINK)
        if skip:
            fields = {"status": DocumentStatus.DUPLICATE.value, "duplicate_of": canonical, "duplicate_policy": policy.value}
            deps.registry.update_document(doc["document_id"], **fields)
            deps.registry.update_member(doc["ingestion_id"], doc["document_id"], status=DocumentStatus.DUPLICATE.value,
                                        duplicate_of=canonical)
        summary = {"exact_duplicate": is_dup, "skip_remaining": skip, "duplicate_of": canonical if is_dup else None,
                   "policy": policy.value}
        return summary, summary, {"exact_duplicate": int(is_dup)}

    # The policy is part of the idempotency key so a changed policy is evaluated again.
    return execute_stage(deps, ctx, doc=doc, version=f"{VERSION}:{policy.value}",
                         out_key=stage_key(ctx.document_id, "metadata", "duplicate.json"), in_key=None,
                         input_checksum=checksum, compute=compute, stage=DocumentStage.DEDUP_CHECKED.value)
