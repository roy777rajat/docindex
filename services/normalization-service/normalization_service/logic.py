"""normalization-service: raw file -> internal pages/blocks schema (processed/.../normalized/normalized.json)."""
from __future__ import annotations

import tempfile
from typing import Any

from cleandocs_shared import utils
from cleandocs_shared.exceptions import UnsupportedFormat, ValidationFailure, classify_exception
from cleandocs_shared.models import DocumentStage, ProcessRequest, ProcessResponse
from cleandocs_shared.pipeline import StageDeps, execute_stage, load_document, stage_key
from botocore.exceptions import ClientError

from . import VERSION, parsers

SCHEMA_VERSION = 1
SERVICE_NAME = "normalization-service"


def out_key(document_id: str) -> str:
    return stage_key(document_id, "normalized", "normalized.json")


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    doc, ctx = load_document(deps, req.context)
    if doc.get("is_archive"):
        raise ValidationFailure("archives are expanded by archive-service, not normalized", code="ARCHIVE_NOT_NORMALIZABLE")
    kind = doc.get("file_kind")
    if not kind:
        raise ValidationFailure("document has not been validated", code="NOT_VALIDATED")
    bucket, key = utils.parse_s3_uri(doc["s3_uri_original"])
    checksum = doc["checksum_sha256"]

    def compute():
        with tempfile.TemporaryFile() as tmp:
            try:
                deps.s3.download_fileobj(bucket, key, tmp)
            except ClientError as exc:
                raise classify_exception(exc) from exc
            tmp.seek(0)
            actual, _ = utils.sha256_stream(tmp)
            if actual != checksum:
                raise ValidationFailure("stored bytes do not match the registered checksum", code="CHECKSUM_MISMATCH")
            tmp.seek(0)
            max_pages = deps.settings.max_pdf_pages
            try:
                if kind == "pdf":
                    pages, info = parsers.parse_pdf(tmp, max_pages)
                elif kind == "docx":
                    pages, info = parsers.parse_docx(tmp)
                elif kind == "xlsx":
                    pages, info = parsers.parse_xlsx(tmp)
                elif kind == "text":
                    pages, info = parsers.parse_text(tmp, doc.get("extension", ""))
                elif kind in ("jpeg", "png", "tiff"):
                    pages, info = parsers.parse_image(tmp, kind, max_pages)
                else:
                    raise UnsupportedFormat(f"no parser for file kind '{kind}'", code="NO_PARSER_FOR_KIND")
            except (UnsupportedFormat, ValidationFailure):
                raise
            except Exception as exc:  # parser library errors on malformed content are not retryable
                raise ValidationFailure(f"could not parse {kind} content: {type(exc).__name__}", code="PARSE_FAILED") from exc
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "document": {"document_id": doc["document_id"], "filename": doc["original_filename"], "kind": kind,
                         "checksum_sha256": checksum, "parent_document_id": doc.get("parent_document_id"),
                         "archive_entry_path": doc.get("archive_entry_path")},
            "parse_info": info, "pages": pages,
        }
        blocks = sum(len(p["blocks"]) for p in pages)
        chars = sum(len(b["text"]) for p in pages for b in p["blocks"])
        ocr = sum(1 for p in pages if p["needs_ocr"])
        summary = {"kind": kind, "pages": len(pages), "blocks": blocks, "chars": chars, "ocr_pages_needed": ocr,
                   "needs_ocr": ocr > 0}
        return data, summary, {"pages": len(pages), "blocks": blocks, "chars": chars}

    return execute_stage(deps, ctx, doc=doc, version=VERSION, out_key=out_key(ctx.document_id), in_key=None,
                         input_checksum=checksum, compute=compute, stage=DocumentStage.NORMALIZED.value)
