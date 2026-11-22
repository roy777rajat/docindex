"""extraction-service: finalise text per page. Text-layer pages pass through; scanned pages go to Textract (guarded)."""
from __future__ import annotations

import io
import json
from typing import Any

from botocore.exceptions import ClientError

from cleandocs_shared import utils
from cleandocs_shared.exceptions import PermanentBusinessFailure, ValidationFailure, classify_exception
from cleandocs_shared.models import DocumentStage, ExecutionStatus, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, read_object, require_input, stage_key
from cleandocs_shared.textclean import clean_text

from . import VERSION

MAX_OCR_BYTES = 10 * 1024 * 1024  # synchronous Textract limit


def out_key(document_id: str) -> str:
    return stage_key(document_id, "extraction", "extraction.json")


def _page_bytes(raw: bytes, kind: str, page_no: int) -> bytes:
    """One page as a single-page document the synchronous Textract API accepts."""
    if kind == "pdf":
        from pypdf import PdfReader, PdfWriter

        w = PdfWriter()
        w.add_page(PdfReader(io.BytesIO(raw)).pages[page_no - 1])
        out = io.BytesIO()
        w.write(out)
        return out.getvalue()
    if kind == "tiff":
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img.seek(page_no - 1)
        out = io.BytesIO()
        img.convert("RGB").save(out, "PNG")
        return out.getvalue()
    return raw  # jpeg / png


def _ocr_page(deps: StageDeps, raw: bytes, kind: str, page_no: int, doc_id: str) -> tuple[str, float | None]:
    payload = _page_bytes(raw, kind, page_no)
    if len(payload) > MAX_OCR_BYTES:
        raise ValidationFailure(f"page {page_no} is larger than the 10 MB OCR limit", code="OCR_PAGE_TOO_LARGE")
    try:
        resp = deps.textract.detect_document_text(Document={"Bytes": payload})
    except ClientError as exc:
        raise classify_exception(exc) from exc
    key = stage_key(doc_id, "extraction/raw", f"textract_p{page_no}.json")
    deps.s3.put_object(Bucket=deps.settings.s3_bucket, Key=key, Body=json.dumps(resp, default=str).encode(),
                       ContentType="application/json")
    lines = [b for b in resp.get("Blocks", []) if b.get("BlockType") == "LINE"]
    conf = [b["Confidence"] for b in lines if "Confidence" in b]
    text = clean_text("\n".join(b.get("Text", "") for b in lines))
    return text, (round(sum(conf) / len(conf), 2) if conf else None)


def progress_key(document_id: str) -> str:
    return stage_key(document_id, "extraction", "ocr_progress.json")


def _load_progress(deps: StageDeps, document_id: str, checksum: str) -> dict[str, Any]:
    raw = read_object(deps, progress_key(document_id))
    if raw:
        prev = json.loads(raw)
        if prev.get("input_checksum") == checksum:
            return prev["pages"]
    return {}


def _mark_failed(deps: StageDeps, doc: dict[str, Any], ctx, err: PermanentBusinessFailure) -> None:
    deps.registry.update_document(ctx.document_id, status="FAILED", error_code=err.code, error_message=err.message[:200])
    deps.registry.update_member(doc["ingestion_id"], ctx.document_id, status="FAILED", error_code=err.code)


def _ocr_batch(deps: StageDeps, doc: dict[str, Any], ctx, env: dict[str, Any], in_checksum: str, in_key: str,
               limit: int) -> ProcessResponse | None:
    """OCR at most `limit` pending pages and persist them. Returns a 'not complete' response when pages remain,
    otherwise None (the caller finishes the stage). Each call is bounded because HTTP Tasks time out after 60 s."""
    settings = deps.settings
    data = env["data"]
    done = _load_progress(deps, ctx.document_id, in_checksum)
    pending = [p for p in data["pages"] if p.get("needs_ocr") and str(p["page"]) not in done]
    if not pending:
        return None
    if not settings.ocr_enabled:
        err = PermanentBusinessFailure("document needs OCR but OCR is disabled", code="OCR_DISABLED")
        _mark_failed(deps, doc, ctx, err)
        raise err
    batch = pending[: max(1, limit)]
    with deps.audit.execution(ctx, input_s3_uri=utils.s3_uri(settings.s3_bucket, in_key), input_checksum=in_checksum) as rec:
        try:
            month = utils.utcnow().strftime("%Y-%m")
            if not deps.registry.reserve_counter(month, "textract_pages", len(batch), settings.textract_monthly_page_cap):
                raise PermanentBusinessFailure("monthly Textract page budget would be exceeded", code="TEXTRACT_BUDGET_EXCEEDED")
            bucket, key = utils.parse_s3_uri(doc["s3_uri_original"])
            try:
                original = deps.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            except ClientError as exc:
                raise classify_exception(exc) from exc
            for page in batch:
                text, conf = _ocr_page(deps, original, data["document"]["kind"], page["page"], ctx.document_id)
                done[str(page["page"])] = {"text": text, "confidence": conf}
        except PermanentBusinessFailure as err:
            _mark_failed(deps, doc, ctx, err)
            raise
        finally:  # keep pages already paid for, even if a later page in the batch failed
            deps.s3.put_object(Bucket=settings.s3_bucket, Key=progress_key(ctx.document_id), ContentType="application/json",
                               Body=json.dumps({"input_checksum": in_checksum, "pages": done}).encode())
        remaining = len([p for p in pending if str(p["page"]) not in done])
        rec.add_metric("ocr_pages", len(batch))
        rec.add_metric("ocr_pages_remaining", remaining)
        if remaining == 0:
            return None
        return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
                               metrics={"ocr_pages": len(batch)},
                               result={"complete": False, "ocr_pages_done": len(done), "ocr_pages_remaining": remaining})


LOW_OCR_CONFIDENCE = 80.0  # Textract confidence is 0-100; below this average the text is flagged as unreliable


def _ocr_fields(summary: dict[str, Any]) -> dict[str, Any]:
    conf = summary.get("avg_ocr_confidence")
    if conf is None:
        return {}
    return {"avg_ocr_confidence": conf, "ocr_low_confidence": conf < LOW_OCR_CONFIDENCE}


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    in_key = stage_key(ctx.document_id, "cleaned", "cleaned.json")
    raw_in, env = require_input(deps, in_key, "cleaned output")
    in_checksum = utils.sha256_bytes(raw_in)
    limit = int(req.parameters.get("ocr_pages_per_call", deps.settings.ocr_pages_per_call))

    partial = _ocr_batch(deps, doc, ctx, env, in_checksum, in_key, limit)
    if partial is not None:
        return partial

    def compute():
        data: dict[str, Any] = env["data"]
        done = _load_progress(deps, ctx.document_id, in_checksum)
        ocr_done, confidences = 0, []
        for page in data["pages"]:
            if page.get("needs_ocr"):
                got = done.get(str(page["page"]))
                if got is None:
                    raise PermanentBusinessFailure("OCR progress is missing for a page", code="OCR_PROGRESS_MISSING")
                text = got["text"]
                page["blocks"] = [{"id": f"p{page['page']}-b1", "type": "paragraph", "text": text}] if text else []
                page["needs_ocr"], page["method"], page["ocr_confidence"] = False, "textract", got["confidence"]
                ocr_done += 1
                if got["confidence"] is not None:
                    confidences.append(got["confidence"])
        chars = sum(len(b["text"]) for p in data["pages"] for b in p["blocks"])
        if chars == 0:
            raise PermanentBusinessFailure("no text could be extracted from the document", code="NO_TEXT_EXTRACTED")
        summary = {"complete": True, "kind": data["document"]["kind"], "pages": len(data["pages"]), "chars": chars,
                   "ocr_pages": ocr_done,
                   "avg_ocr_confidence": round(sum(confidences) / len(confidences), 2) if confidences else None}
        return data, summary, {"pages": len(data["pages"]), "chars": chars, "ocr_pages": ocr_done}

    return execute_stage(deps, ctx, doc=doc, version=VERSION, out_key=out_key(ctx.document_id), in_key=in_key,
                         input_checksum=in_checksum, compute=compute, stage=DocumentStage.EXTRACTED.value,
                         extra_doc_fields=_ocr_fields)
