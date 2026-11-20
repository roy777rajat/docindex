"""Archive extraction: stream each safe entry to S3 as an independent child document."""
from __future__ import annotations

import json
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from cleandocs_shared import utils
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.exceptions import CleanDocsError, NotFound, ValidationFailure, classify_exception
from cleandocs_shared.filetypes import ALLOWED_EXTENSIONS, detect_kind, extension_matches_kind, extension_of
from cleandocs_shared.logging_utils import get_logger
from cleandocs_shared.models import DocumentStage, DocumentStatus, ExecutionStatus, ProcessResponse, TraceContext
from cleandocs_shared.registry import Registry

from . import VERSION
from .safety import ArchiveFatal, child_document_id, is_ignored, normalize_entry_path, prescan

log = get_logger(__name__)
_BLOCK = 1024 * 1024


@dataclass
class ArchiveDeps:
    settings: Settings
    s3: Any
    registry: Registry
    audit: AuditWriter


def manifest_key(parent_id: str) -> str:
    return utils.processed_key(parent_id, "manifests", "archive_manifest.json")


class _EntryRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _copy_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, tmp, settings: Settings, total_so_far: int) -> tuple[str, int]:
    """Stream one entry to `tmp` counting REAL bytes (headers can lie). Returns (sha256, size)."""
    import hashlib

    h, size = hashlib.sha256(), 0
    with zf.open(info) as src:
        while True:
            block = src.read(_BLOCK)
            if not block:
                break
            size += len(block)
            if total_so_far + size > settings.max_archive_expanded_bytes:
                raise ArchiveFatal("archive expanded beyond the allowed total size", code="ARCHIVE_LIMIT_EXCEEDED")
            if size > settings.max_file_size_bytes:
                raise _EntryRejected("FILE_TOO_LARGE")
            h.update(block)
            tmp.write(block)
    tmp.seek(0)
    return h.hexdigest(), size


def _put_child(deps: ArchiveDeps, key: str, tmp, size: int, checksum: str, child_id: str) -> None:
    import base64

    try:
        deps.s3.put_object(
            Bucket=deps.settings.s3_bucket, Key=key, Body=tmp, ContentLength=size, ContentType="application/octet-stream",
            ChecksumSHA256=base64.b64encode(bytes.fromhex(checksum)).decode(),
            IfNoneMatch="*", Metadata={"document-id": child_id, "sha256": checksum},
        )
    except ClientError as exc:
        # 412: the object exists from an earlier (partial) run of this same extraction: idempotent success
        if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            return
        raise classify_exception(exc) from exc


def run_extraction(deps: ArchiveDeps, ctx: TraceContext, input_s3_uri: str | None) -> ProcessResponse:
    s, reg = deps.settings, deps.registry
    if not ctx.document_id:
        raise ValidationFailure("context.document_id is required", code="DOCUMENT_ID_REQUIRED")
    doc = reg.get_document(ctx.document_id)
    if doc is None:
        raise NotFound("document is not registered", code="DOCUMENT_NOT_FOUND")
    if not doc.get("is_archive"):
        raise ValidationFailure("document was not validated as an archive", code="NOT_AN_ARCHIVE")

    uri = input_s3_uri or doc["s3_uri_original"]
    bucket, key = utils.parse_s3_uri(uri)
    if bucket != s.s3_bucket:
        raise ValidationFailure("input is outside the CleanDocs bucket", code="FOREIGN_BUCKET")
    parent_id, ingestion_id = ctx.document_id, doc["ingestion_id"]
    ctx = ctx.model_copy(update={"ingestion_id": ctx.ingestion_id or ingestion_id})
    depth = int(doc.get("archive_depth", 0))
    mkey = manifest_key(parent_id)

    with deps.audit.execution(ctx, input_s3_uri=uri, input_checksum=doc["checksum_sha256"]) as rec:
        if doc["stage"] == DocumentStage.EXTRACTED_ARCHIVE.value:
            try:
                existing = json.loads(deps.s3.get_object(Bucket=bucket, Key=mkey)["Body"].read())
            except ClientError:
                existing = None
            if existing and existing.get("archive_checksum") == doc["checksum_sha256"] and existing.get("service_version") == VERSION:
                rec.skip("archive already extracted for this checksum and service version")
                return ProcessResponse(
                    service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                    output_s3_uri=utils.s3_uri(bucket, mkey), skipped_reason=rec.skipped_reason, result=existing["summary"],
                )

        entries: list[dict[str, Any]] = []
        try:
            with tempfile.TemporaryFile() as zip_tmp:
                try:
                    deps.s3.download_fileobj(bucket, key, zip_tmp)
                except ClientError as exc:
                    raise classify_exception(exc) from exc
                zip_tmp.seek(0)
                actual, _ = utils.sha256_stream(zip_tmp)
                if actual != doc["checksum_sha256"]:
                    raise ValidationFailure("stored bytes do not match the registered checksum", code="CHECKSUM_MISMATCH")
                zip_tmp.seek(0)
                try:
                    zf = zipfile.ZipFile(zip_tmp)
                except zipfile.BadZipFile as exc:
                    raise ValidationFailure("archive is corrupt", code="CORRUPT_FILE") from exc
                with zf:
                    files = prescan(
                        zf.infolist(), max_files=s.max_archive_file_count, max_expanded=s.max_archive_expanded_bytes,
                        max_file_size=s.max_file_size_bytes, max_ratio=s.max_archive_compression_ratio,
                    )
                    seen: set[str] = set()
                    total = 0
                    for info in files:
                        path = normalize_entry_path(info.filename)
                        cid = child_document_id(parent_id, path)
                        base: dict[str, Any] = {"entry_path": path, "document_id": cid}
                        if is_ignored(path):
                            entries.append({**base, "status": "IGNORED", "reason": "JUNK_FILE"})
                            continue
                        if path in seen:
                            entries.append({**base, "status": "REJECTED", "reason": "DUPLICATE_ENTRY_PATH"})
                            continue
                        seen.add(path)
                        name = path.rsplit("/", 1)[-1]
                        ext = extension_of(name)
                        if ext not in ALLOWED_EXTENSIONS:
                            entries.append({**base, "status": "REJECTED", "reason": "EXTENSION_NOT_ALLOWED"})
                            continue
                        with tempfile.TemporaryFile() as tmp:
                            try:
                                checksum, size = _copy_entry(zf, info, tmp, s, total)
                            except _EntryRejected as rej:
                                entries.append({**base, "status": "REJECTED", "reason": rej.code})
                                continue
                            total += size
                            if size == 0:
                                entries.append({**base, "status": "REJECTED", "reason": "EMPTY_FILE"})
                                continue
                            kind = detect_kind(tmp)
                            if not extension_matches_kind(ext, kind):
                                entries.append({**base, "status": "REJECTED", "reason": "SIGNATURE_MISMATCH"})
                                continue
                            if kind == "zip" and depth + 1 > s.max_archive_depth:
                                entries.append({**base, "status": "REJECTED", "reason": "NESTED_ARCHIVE_NOT_ALLOWED"})
                                continue
                            tmp.seek(0)
                            child_key = utils.raw_original_key(cid, name)
                            _put_child(deps, child_key, tmp, size, checksum, cid)
                            child_uri = utils.s3_uri(bucket, child_key)
                            reg.create_document(
                                document_id=cid, ingestion_id=ingestion_id, original_filename=utils.safe_filename(name),
                                extension=ext, size_bytes=size, checksum_sha256=checksum, s3_uri_original=child_uri,
                                parent_document_id=parent_id, archive_entry_path=path, archive_depth=depth + 1,
                            )
                            entries.append({**base, "status": "ACCEPTED", "s3_uri": child_uri, "size_bytes": size,
                                            "checksum_sha256": checksum, "file_kind": kind})
        except CleanDocsError as err:
            if not err.retryable:
                reg.update_document(parent_id, status=DocumentStatus.REJECTED.value, rejection_code=err.code,
                                    rejection_reason=err.message[:200])
                reg.update_member(ingestion_id, parent_id, status=DocumentStatus.REJECTED.value, rejection_code=err.code)
            raise

        accepted = [e for e in entries if e["status"] == "ACCEPTED"]
        rejected = [e for e in entries if e["status"] == "REJECTED"]
        for e in entries:  # every entry is accounted for in the ingestion manifest
            if e["status"] == "ACCEPTED":
                reg.add_member(ingestion_id=ingestion_id, document_id=e["document_id"], role="FILE",
                               parent_document_id=parent_id, entry_path=e["entry_path"], s3_uri=e["s3_uri"],
                               checksum_sha256=e["checksum_sha256"], original_filename=e["entry_path"].rsplit("/", 1)[-1])
            elif e["status"] == "REJECTED":
                reg.add_member(ingestion_id=ingestion_id, document_id=e["document_id"], role="FILE",
                               status=DocumentStatus.REJECTED.value, parent_document_id=parent_id,
                               entry_path=e["entry_path"], rejection_code=e["reason"])

        summary = {
            "parent_document_id": parent_id, "accepted": len(accepted), "rejected": len(rejected),
            "ignored": sum(1 for e in entries if e["status"] == "IGNORED"),
            "children": [{k: e[k] for k in ("document_id", "s3_uri", "checksum_sha256", "entry_path")} for e in accepted],
            "rejected_entries": [{"entry_path": e["entry_path"], "reason": e["reason"]} for e in rejected],
        }
        manifest = {
            "parent_document_id": parent_id, "ingestion_id": ingestion_id, "archive_checksum": doc["checksum_sha256"],
            "service_version": VERSION, "created_at": utils.iso(), "entries": entries, "summary": summary,
        }
        body = json.dumps(manifest, separators=(",", ":")).encode()
        deps.s3.put_object(Bucket=bucket, Key=mkey, Body=body, ContentType="application/json")
        out_uri = utils.s3_uri(bucket, mkey)

        reg.update_document(parent_id, stage=DocumentStage.EXTRACTED_ARCHIVE.value, archive_manifest_uri=out_uri,
                            child_count=len(accepted))
        reg.update_ingestion(ingestion_id, archive_manifest_uri=out_uri)
        rec.set_output(out_uri, utils.sha256_bytes(body))
        rec.add_metric("children_accepted", len(accepted))
        rec.add_metric("entries_rejected", len(rejected))

        if not accepted:  # evidence is stored first, then the archive is rejected as a whole
            reg.update_document(parent_id, status=DocumentStatus.REJECTED.value, rejection_code="NO_SUPPORTED_FILES")
            reg.update_member(ingestion_id, parent_id, status=DocumentStatus.REJECTED.value, rejection_code="NO_SUPPORTED_FILES")
            raise ValidationFailure("archive contains no supported files", code="NO_SUPPORTED_FILES",
                                    details={"rejected": len(rejected)})
        return ProcessResponse(
            service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED, output_s3_uri=out_uri,
            output_checksum=utils.sha256_bytes(body), metrics={"children": len(accepted)}, result=summary,
        )
