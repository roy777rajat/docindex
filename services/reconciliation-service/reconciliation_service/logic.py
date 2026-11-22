"""reconciliation-service: decide each document's and the ingestion's final status from DURABLE EVIDENCE.

An HTTP 200 or a registry field is a claim, not proof. For every document this service re-checks:
  original   the raw object exists in S3, has the registered size and SHA-256
  artifacts  every stage output exists, parses, and belongs to this document
  chunks     every chunk text + citation sidecar object exists (optionally re-hashed); no orphan objects
  indexing   the Knowledge Base itself lists every chunk as INDEXED (and holds no stale entries for the document)
  audit      every required service has a terminal COMPLETED/SKIPPED row in the immutable trail
  graph      graph_status matches what the trail shows (NOT_IMPLEMENTED / NOT_CONFIGURED / FAILED, never an invented COMPLETED)

Verdict: a failed `required` check (data missing) -> FAILED; a failed `indexing` or `audit` check -> PARTIAL; otherwise
COMPLETED. DUPLICATE, REJECTED and FAILED documents are verified but never upgraded. Documents still running stay PROCESSING.

Endpoints
  POST /api/v1/process                              reconcile every file of an ingestion, then the ingestion (time-budgeted, resumable)
  POST /api/v1/documents/{id}/reconcile?repair=...  re-check one document (operator tool)
  GET  /api/v1/documents/{id}/reconciliation        stored findings for one document
  GET  /api/v1/ingestions/{id}/reconciliation       stored report for one ingestion
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cleandocs_shared import utils
from cleandocs_shared.exceptions import NotFound, ValidationFailure, classify_exception
from cleandocs_shared.kb import kb_document_statuses
from cleandocs_shared.models import DocumentStage, DocumentStatus, ExecutionStatus, ProcessRequest, ProcessResponse, TraceContext
from cleandocs_shared.pipeline import StageDeps, read_object, stage_key
from cleandocs_shared.rollup import rollup

from . import VERSION

TERMINAL_AUDIT = {"COMPLETED", "FAILED", "SKIPPED", "NOT_IMPLEMENTED"}
OK_AUDIT = {"COMPLETED", "SKIPPED"}
FINAL_DOC = {"COMPLETED", "PARTIAL", "FAILED", "DUPLICATE", "REJECTED"}
# (service, stage dir, file, is a stage envelope, only for documents uploaded directly, i.e. not archive entries)
PIPELINE = [
    ("validation", "metadata", "validation.json", False, True),
    ("normalization", "normalized", "normalized.json", True, False),
    ("cleaning", "cleaned", "cleaned.json", True, False),
    ("duplicate", "metadata", "duplicate.json", True, False),
    ("extraction", "extraction", "extraction.json", True, False),
    ("metadata", "metadata", "metadata.json", True, False),
    ("chunking", "chunks", "chunks.json", True, False),
    ("embedding", "manifests", "chunks.json", True, False),
]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repair: bool = False                 # delete orphan chunk objects (never deletes anything else)
    force: bool = False                  # re-check documents already reconciled for this indexing run
    verify_chunk_text: bool = False      # re-read every chunk and compare its SHA-256 with the manifest
    max_seconds: int = Field(default=40, ge=5, le=50)  # Step Functions HTTP Tasks are cut off at 60 s


@dataclass
class Finding:
    check: str
    ok: bool
    tier: str  # required | indexing | audit | info
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "ok": self.ok, "tier": self.tier, "detail": self.detail}


@dataclass
class Run:
    """Per-request caches so one call lists the KB once and reads each audit partition once."""
    deps: StageDeps
    params: Params
    kb_status: dict[str, str]
    trails: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


def parse_params(raw: dict[str, Any]) -> Params:
    try:
        return Params.model_validate(raw or {})
    except ValidationError as exc:
        raise ValidationFailure("invalid reconciliation parameters", code="PARAMS_INVALID",
                                details={"fields": [".".join(map(str, e["loc"])) for e in exc.errors()]}) from exc


# ---------------------------------------------------------------- evidence helpers
def _head(deps: StageDeps, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return deps.s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise classify_exception(exc) from exc


def _list_keys(deps: StageDeps, prefix: str) -> set[str]:
    keys, token = set(), None
    while True:
        kw: dict[str, Any] = {"Bucket": deps.settings.s3_bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = deps.s3.list_objects_v2(**kw)
        keys.update(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            return keys
        token = resp["NextContinuationToken"]


def _trail(run: Run, pk: str) -> dict[str, dict[str, Any]]:
    """{service_name: {"terminal": latest terminal row, "orphan": started-but-never-finished newest execution?}}."""
    if pk in run.trails:
        return run.trails[pk]
    rows: list[dict[str, Any]] = []
    kw: dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(pk)}
    table = run.deps.audit._table  # noqa: SLF001
    while True:
        resp = table.query(**kw)
        rows.extend(utils.from_dynamo(i) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    rows.sort(key=lambda r: r["SK"])  # SK starts with the start timestamp, so this is chronological
    finished = {r["service_execution_id"] for r in rows if r["status"] in TERMINAL_AUDIT}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = out.setdefault(r["service_name"], {"terminal": None, "orphan": False})
        if r["status"] in TERMINAL_AUDIT:
            entry["terminal"] = r
        elif r["status"] == "STARTED":
            entry["orphan"] = r["service_execution_id"] not in finished
    run.trails[pk] = out
    return out


def _check_original(run: Run, doc: dict[str, Any], out: list[Finding]) -> None:
    uri = doc.get("s3_uri_original")
    if not uri:
        out.append(Finding("original_exists", False, "required", "document has no original location"))
        return
    bucket, key = utils.parse_s3_uri(uri)
    head = _head(run.deps, bucket, key)
    if head is None:
        out.append(Finding("original_exists", False, "required", "original object is missing from S3"))
        return
    if head["ContentLength"] != doc.get("size_bytes"):
        out.append(Finding("original_exists", False, "required",
                           f"size {head['ContentLength']} differs from registered {doc.get('size_bytes')}"))
        return
    body = run.deps.s3.get_object(Bucket=bucket, Key=key)["Body"]
    digest, _ = utils.sha256_stream(body.iter_chunks(1024 * 1024))
    same = digest == doc.get("checksum_sha256")
    out.append(Finding("original_exists", same, "required", "" if same else "SHA-256 differs from the registered checksum"))


def _check_artifacts(run: Run, doc: dict[str, Any], out: list[Finding]) -> dict[str, Any] | None:
    """Returns the embedding manifest (chunks list) when it is present and valid."""
    deps, did, manifest = run.deps, doc["document_id"], None
    top_level = not doc.get("parent_document_id")
    for service, stage_dir, filename, envelope, only_top in PIPELINE:
        if only_top and not top_level:
            continue
        name = f"artifact_{service}"
        raw = read_object(deps, stage_key(did, stage_dir, filename))
        if raw is None:
            out.append(Finding(name, False, "required", f"{stage_dir}/{filename} is missing"))
            continue
        try:
            body = json.loads(raw)
        except ValueError:
            out.append(Finding(name, False, "required", f"{stage_dir}/{filename} is not valid JSON"))
            continue
        if envelope and (body.get("document_id") != did or not body.get("service_version")):
            out.append(Finding(name, False, "required", f"{stage_dir}/{filename} does not belong to this document or has no version"))
            continue
        out.append(Finding(name, True, "required"))
        if service == "embedding":
            manifest = body
    return manifest


REQUIRED_SIDECAR_KEYS = ("document_id", "chunk_id", "owner", "doc_type", "filename", "ingestion_id")


def _check_sidecars(deps: Any, doc: dict[str, Any], chunks: list[dict[str, Any]], out: list[Finding]) -> None:
    """Every sidecar must carry the registry owner (access control depends on it) and the required keys."""
    want_owner = utils.doc_owner(doc)
    wrong_owner = incomplete = 0
    for c in chunks:
        raw = read_object(deps, utils.parse_s3_uri(c["metadata_s3_uri"])[1])
        try:
            attrs = json.loads(raw)["metadataAttributes"] if raw else {}
        except (ValueError, KeyError, TypeError):
            attrs = {}
        if attrs.get("owner") != want_owner:
            wrong_owner += 1
        if any(k not in attrs for k in REQUIRED_SIDECAR_KEYS):
            incomplete += 1
    out.append(Finding("sidecar_owner_matches_registry", not wrong_owner, "required",
                       f"{wrong_owner} sidecar(s) have a different owner than the registry ({want_owner})" if wrong_owner else ""))
    out.append(Finding("sidecar_attributes_present", not incomplete, "indexing",
                       f"{incomplete} sidecar(s) miss required attributes" if incomplete else ""))


def _check_chunks(run: Run, doc: dict[str, Any], manifest: dict[str, Any] | None, out: list[Finding]) -> None:
    deps, did = run.deps, doc["document_id"]
    if manifest is None:
        out.append(Finding("chunk_objects", False, "required", "chunk manifest is unavailable"))
        return
    chunks = manifest["data"]["chunks"]
    if not chunks:
        out.append(Finding("chunk_objects", False, "required", "document has no chunks"))
        return
    expected: set[str] = set()
    for c in chunks:
        expected.add(utils.parse_s3_uri(c["s3_uri"])[1])
        expected.add(utils.parse_s3_uri(c["metadata_s3_uri"])[1])
    present = _list_keys(deps, f"kb-source/{did}/")
    missing, orphans = expected - present, present - expected
    out.append(Finding("chunk_objects", not missing, "required",
                       f"{len(missing)} of {len(expected)} chunk objects are missing" if missing else f"{len(expected)} objects present"))
    if run.params.verify_chunk_text and not missing:
        bad = 0
        for c in chunks:
            raw = read_object(deps, utils.parse_s3_uri(c["s3_uri"])[1])
            if raw is None or utils.sha256_bytes(raw) != c["text_sha256"]:
                bad += 1
        out.append(Finding("chunk_text_integrity", not bad, "required", f"{bad} chunk text(s) differ from the manifest" if bad else ""))
    _check_sidecars(deps, doc, chunks, out)
    removed = 0
    if orphans and run.params.repair:
        for key in sorted(orphans):
            deps.s3.delete_object(Bucket=deps.settings.s3_bucket, Key=key)
            removed += 1
        orphans = set()
    out.append(Finding("orphan_chunk_objects", not orphans, "indexing",
                       f"{len(orphans)} object(s) not in the manifest (re-run with repair=true)" if orphans
                       else f"removed {removed} orphan object(s)" if removed else ""))
    uris = [c["s3_uri"] for c in chunks]
    not_indexed = [u for u in uris if run.kb_status.get(u) != "INDEXED"]
    out.append(Finding("chunks_indexed", not not_indexed, "indexing",
                       f"{len(not_indexed)} of {len(uris)} chunks are not INDEXED in the knowledge base" if not_indexed
                       else f"{len(uris)} chunks INDEXED"))
    prefix = f"s3://{deps.settings.s3_bucket}/kb-source/{did}/"
    stale = [u for u in run.kb_status if u.startswith(prefix) and u not in set(uris)]
    out.append(Finding("stale_kb_entries", not stale, "indexing",
                       f"{len(stale)} knowledge-base entries have no chunk in the manifest (a new indexing job removes them)" if stale else ""))
    out.append(Finding("chunk_count_consistent", doc.get("kb_chunk_count") in (None, len(chunks)) and manifest["data"].get("chunk_count") == len(chunks),
                       "indexing", f"registry says {doc.get('kb_chunk_count')}, manifest holds {len(chunks)}"))


def _check_audit(run: Run, doc: dict[str, Any], out: list[Finding]) -> None:
    did, top_level = doc["document_id"], not doc.get("parent_document_id")
    trail = _trail(run, utils.doc_pk(did))
    for service, *_rest, only_top in PIPELINE:
        if only_top and not top_level:
            continue
        row = (trail.get(f"{service}-service") or {}).get("terminal")
        ok = bool(row) and row["status"] in OK_AUDIT
        out.append(Finding(f"audit_{service}", ok, "audit",
                           "" if ok else "no terminal COMPLETED/SKIPPED record" if not row else f"latest record is {row['status']}"))
    if not top_level:  # archive entries are validated by the archive extraction of their parent
        row = (_trail(run, utils.doc_pk(doc["parent_document_id"])).get("archive-service") or {}).get("terminal")
        ok = bool(row) and row["status"] in OK_AUDIT
        out.append(Finding("audit_archive_parent", ok, "audit", "" if ok else "parent has no completed archive extraction record"))


def _check_registry(doc: dict[str, Any], out: list[Finding]) -> None:
    stage_ok = doc.get("stage") in (DocumentStage.INDEXED.value, DocumentStage.RECONCILED.value)
    out.append(Finding("registry_stage", stage_ok, "indexing", f"stage is {doc.get('stage')}" if not stage_ok else ""))
    idx_ok = doc.get("indexing_state") == "INDEXED"
    out.append(Finding("registry_indexing_state", idx_ok, "indexing", f"indexing_state is {doc.get('indexing_state')}" if not idx_ok else ""))


def _graph(run: Run, doc: dict[str, Any], out: list[Finding]) -> str:
    row = (_trail(run, utils.doc_pk(doc["document_id"])).get("graph-service") or {}).get("terminal")
    expected = "NOT_CONFIGURED" if not row else {"NOT_IMPLEMENTED": "NOT_IMPLEMENTED", "FAILED": "FAILED"}.get(row["status"], "NOT_IMPLEMENTED")
    current = doc.get("graph_status")
    if current != expected:
        run.deps.registry.update_document(doc["document_id"], graph_status=expected)
        out.append(Finding("graph_status", True, "info", f"corrected {current} -> {expected} to match the audit trail"))
    else:
        out.append(Finding("graph_status", True, "info", expected))
    return expected


def verdict_of(findings: list[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    if any(f.tier == "required" for f in bad):
        return "FAILED"
    return "PARTIAL" if bad else "COMPLETED"


# ---------------------------------------------------------------- one document
def evaluate_document(run: Run, doc: dict[str, Any]) -> dict[str, Any]:
    status, findings = doc.get("status"), []
    did, graph = doc["document_id"], doc.get("graph_status")
    if status == "DUPLICATE":
        _check_original(run, doc, findings)
        raw = read_object(run.deps, stage_key(did, "metadata", "duplicate.json"))
        info = (json.loads(raw).get("data") or {}) if raw else {}
        findings.append(Finding("duplicate_evidence", bool(info.get("exact_duplicate") and info.get("skip_remaining")), "required",
                                "" if info else "duplicate decision artifact is missing"))
        canonical = run.deps.registry.get_document(doc.get("duplicate_of") or "")
        ok = canonical is not None and canonical.get("status") not in ("FAILED", "REJECTED")
        findings.append(Finding("canonical_document", ok, "required", "" if ok else "the document it duplicates is missing or failed"))
        v = "DUPLICATE" if all(f.ok for f in findings) else "FAILED"
    elif status in ("REJECTED", "FAILED"):
        _check_original(run, doc, findings)
        for f in findings:
            f.tier = "info"  # the verdict is already negative; a missing original is only noted
        row = None
        for svc, entry in _trail(run, utils.doc_pk(did)).items():
            t = entry.get("terminal")
            if t and t["status"] == "FAILED" and (row is None or t["SK"] > row["SK"]):
                row = t
        findings.append(Finding("failure_evidence", bool(doc.get("error_code") or row), "info",
                                f"{(row or {}).get('service_name', 'registry')}: {doc.get('error_code') or (row or {}).get('error_code', '')}"))
        v = status
    elif status in ("COMPLETED", "PARTIAL") or doc.get("stage") in (DocumentStage.INDEXED.value, DocumentStage.RECONCILED.value):
        _check_original(run, doc, findings)
        manifest = _check_artifacts(run, doc, findings)
        _check_chunks(run, doc, manifest, findings)
        _check_audit(run, doc, findings)
        _check_registry(doc, findings)
        graph = _graph(run, doc, findings)
        v = verdict_of(findings)
    else:
        return {"document_id": did, "verdict": "PROCESSING", "findings": [], "graph_status": doc.get("graph_status")}
    return {"document_id": did, "verdict": v, "findings": [f.as_dict() for f in findings], "graph_status": graph}


def _apply(deps: StageDeps, doc: dict[str, Any], result: dict[str, Any], token: str) -> None:
    v, did = result["verdict"], doc["document_id"]
    fields: dict[str, Any] = {"reconciliation": {"verdict": v, "run": token, "checked_at": utils.iso(), "findings": result["findings"]},
                              "reconciled_at": utils.iso()}
    if v in FINAL_DOC and doc.get("status") in ("COMPLETED", "PARTIAL", "PROCESSING") and v != doc.get("status"):
        fields["status"] = v
        if v == "FAILED":
            fields["error_code"] = "RECONCILIATION_EVIDENCE_MISSING"
    if v in ("COMPLETED", "PARTIAL"):
        fields["stage"] = DocumentStage.RECONCILED.value
    deps.registry.update_document(did, **fields)
    if "status" in fields:
        member = {"status": fields["status"]}
        if "error_code" in fields:
            member["error_code"] = fields["error_code"]
        deps.registry.update_member(doc["ingestion_id"], did, **member)


# ---------------------------------------------------------------- one ingestion
def _kb(deps: StageDeps) -> tuple[str, str]:
    kb, ds = deps.settings.bedrock_knowledge_base_id, deps.settings.bedrock_data_source_id
    if not kb or not ds:
        raise ValidationFailure("knowledge base is not configured", code="KB_NOT_CONFIGURED")
    return kb, ds


def _make_run(deps: StageDeps, params: Params) -> Run:
    kb, ds = _kb(deps)
    try:
        return Run(deps, params, kb_document_statuses(deps.bedrock_agent, kb, ds))
    except ClientError as exc:
        raise classify_exception(exc) from exc


def _archive_children(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Children accepted by archive-service. The file it writes keeps them under summary.children."""
    return (manifest.get("summary") or {}).get("children") or manifest.get("children") or []


def _ingestion_checks(run: Run, ing: dict[str, Any], members: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> list[Finding]:
    out, ids = [], {m["document_id"] for m in members}
    for m in (m for m in members if m.get("role") == "ARCHIVE"):
        raw = read_object(run.deps, utils.processed_key(m["document_id"], "manifests", "archive_manifest.json"))
        if raw is None:
            out.append(Finding(f"archive_manifest:{m['document_id']}", False, "required", "archive manifest is missing"))
            continue
        children = [c["document_id"] for c in _archive_children(json.loads(raw))]
        lost = [c for c in children if c not in ids or c not in docs]
        out.append(Finding(f"archive_children:{m['document_id']}", not lost, "required",
                           f"{len(lost)} of {len(children)} extracted files are not tracked in this ingestion" if lost
                           else f"{len(children)} extracted files accounted for"))
    if ing.get("kb_job_id"):
        out.append(Finding("kb_job_complete", ing.get("kb_job_status") == "COMPLETE", "indexing", f"job status {ing.get('kb_job_status')}"))
        row = (_trail(run, utils.ingestion_pk(ing["ingestion_id"])).get("vector-indexing-service") or {}).get("terminal")
        out.append(Finding("audit_vector_indexing", bool(row) and row["status"] in OK_AUDIT, "audit", "" if row else "no indexing audit record"))
    return out


def _archive_statuses(deps: StageDeps, members: list[dict[str, Any]], docs: dict[str, dict[str, Any]]) -> None:
    for a in (m for m in members if m.get("role") == "ARCHIVE"):
        kids = [m for m in members if m.get("role", "FILE") == "FILE" and (docs.get(m["document_id"]) or {}).get("parent_document_id") == a["document_id"]]
        if not kids:
            continue
        status = rollup(kids)["status"]
        deps.registry.update_member(a["ingestion_id"], a["document_id"], status=status)
        deps.registry.update_document(a["document_id"], status=status)


def _docs(deps: StageDeps, members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["document_id"]: d for m in members if (d := deps.registry.get_document(m["document_id"]))}


def run(deps: StageDeps, req: ProcessRequest) -> ProcessResponse:
    ctx = req.context
    if not ctx.ingestion_id:
        raise ValidationFailure("context.ingestion_id is required", code="INGESTION_ID_REQUIRED")
    params = parse_params(req.parameters)
    t0 = time.monotonic()
    with deps.audit.execution(ctx) as rec:
        ing = deps.registry.get_ingestion(ctx.ingestion_id)
        if ing is None:
            raise NotFound("ingestion not found", code="INGESTION_NOT_FOUND")
        members = deps.registry.list_members(ctx.ingestion_id)
        files = sorted((m for m in members if m.get("role", "FILE") == "FILE"), key=lambda m: m["document_id"])
        if not files:
            rec.skip("the ingestion has no files to reconcile")
            return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.SKIPPED,
                                   skipped_reason=rec.skipped_reason, result={"complete": True, "status": ing.get("status"), "documents": 0})
        rr = _make_run(deps, params)
        token = ing.get("kb_job_id") or "no-index-job"   # a new indexing job means the evidence must be re-checked
        checked = 0
        for m in files:
            doc = deps.registry.get_document(m["document_id"])
            if doc is None:
                continue
            if not params.force and (doc.get("reconciliation") or {}).get("run") == token:
                continue
            if checked and time.monotonic() - t0 > params.max_seconds:
                break
            _apply(deps, doc, evaluate_document(rr, doc), token)
            checked += 1
        remaining = sum(1 for m in files if (deps.registry.get_document(m["document_id"]) or {}).get("reconciliation", {}).get("run") != token)
        rec.add_metric("documents_checked", checked)
        if remaining:
            return ProcessResponse(service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED,
                                   metrics={"documents_checked": checked},
                                   result={"complete": False, "documents_checked": checked, "documents_remaining": remaining})
        return _finalize(deps, rr, rec, ctx, ing, token, checked)


def _finalize(deps: StageDeps, rr: Run, rec: Any, ctx: TraceContext, ing: dict[str, Any], token: str, checked: int) -> ProcessResponse:
    members = deps.registry.list_members(ctx.ingestion_id)
    docs = _docs(deps, members)
    extra = _ingestion_checks(rr, ing, members, docs)
    _archive_statuses(deps, members, docs)
    r = rollup(members)
    status = r["status"]
    if status == "COMPLETED" and any(not f.ok for f in extra):
        status = "PARTIAL"
    finalized = r["all_terminal"]
    per_doc = []
    for m in sorted((m for m in members if m.get("role", "FILE") == "FILE"), key=lambda m: m["document_id"]):
        d = docs.get(m["document_id"], {})
        rc = d.get("reconciliation") or {}
        per_doc.append({"document_id": m["document_id"], "status": m["status"], "graph_status": d.get("graph_status"),
                        "verdict": rc.get("verdict"), "findings": rc.get("findings", [])})
    problems = [{"document_id": d["document_id"], "status": d["status"], "failed_checks": [f["check"] for f in d["findings"] if not f["ok"]]}
                for d in per_doc if d["status"] not in ("COMPLETED", "DUPLICATE") or any(not f["ok"] for f in d["findings"])]
    report = {"ingestion_id": ctx.ingestion_id, "status": status if finalized else "PROCESSING", "finalized": finalized,
              "reconciled_at": utils.iso(), "service_version": VERSION, "run": token, "counts": r["counts"],
              "ingestion_checks": [f.as_dict() for f in extra], "documents": per_doc}
    key = f"processed/ingestions/{ctx.ingestion_id}/reconciliation.json"
    body = json.dumps(report, separators=(",", ":"), ensure_ascii=False).encode()
    deps.s3.put_object(Bucket=deps.settings.s3_bucket, Key=key, Body=body, ContentType="application/json")
    uri = utils.s3_uri(deps.settings.s3_bucket, key)
    if finalized:
        deps.registry.update_ingestion(ctx.ingestion_id, status=status, reconciliation_status=status, reconciled_at=utils.iso(),
                                       reconciliation_report_uri=uri)
    rec.set_output(uri, utils.sha256_bytes(body))
    rec.add_metric("problems", len(problems))
    return ProcessResponse(
        service_execution_id=rec.service_execution_id, status=ExecutionStatus.COMPLETED, output_s3_uri=uri,
        output_checksum=utils.sha256_bytes(body), metrics={"documents_checked": checked, "problems": len(problems)},
        result={"complete": True, "finalized": finalized, "status": report["status"], "counts": r["counts"],
                "problems": problems[:20], "problem_count": len(problems), "report_uri": uri})


# ---------------------------------------------------------------- operator routes
def add_routes(app: FastAPI, deps: StageDeps, auth: Callable[..., None]) -> None:
    @app.post("/api/v1/documents/{document_id}/reconcile", dependencies=[Depends(auth)])
    def reconcile_one(document_id: str, repair: bool = False, verify_chunk_text: bool = False) -> dict[str, Any]:
        doc = deps.registry.get_document(document_id)
        if doc is None:
            raise NotFound("document is not registered", code="DOCUMENT_NOT_FOUND")
        ctx = TraceContext(document_id=document_id, ingestion_id=doc["ingestion_id"])
        with deps.audit.execution(ctx):
            rr = _make_run(deps, Params(repair=repair, verify_chunk_text=verify_chunk_text))
            result = evaluate_document(rr, doc)
            ing = deps.registry.get_ingestion(doc["ingestion_id"]) or {}
            _apply(deps, doc, result, ing.get("kb_job_id") or "no-index-job")
            return result

    @app.get("/api/v1/documents/{document_id}/reconciliation", dependencies=[Depends(auth)])
    def stored_document(document_id: str) -> dict[str, Any]:
        doc = deps.registry.get_document(document_id)
        if doc is None or "reconciliation" not in doc:
            raise NotFound("no reconciliation result for this document", code="RECONCILIATION_NOT_FOUND")
        return {"document_id": document_id, "status": doc["status"], "graph_status": doc.get("graph_status"), **doc["reconciliation"]}

    @app.get("/api/v1/ingestions/{ingestion_id}/reconciliation", dependencies=[Depends(auth)])
    def stored_ingestion(ingestion_id: str) -> dict[str, Any]:
        raw = read_object(deps, f"processed/ingestions/{ingestion_id}/reconciliation.json")
        if raw is None:
            raise NotFound("this ingestion has not been reconciled", code="RECONCILIATION_NOT_FOUND")
        return json.loads(raw)
