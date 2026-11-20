"""Reconciliation decides statuses from durable evidence (S3 objects, checksums, trail rows, Knowledge Base listing)."""
import io
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
for s in ("normalization", "cleaning", "duplicate", "extraction", "metadata", "chunking", "embedding", "vector-indexing",
          "reconciliation", "manifest"):
    sys.path.insert(0, str(ROOT / "services" / f"{s}-service"))

from chunking_service import main as chunking  # noqa: E402
from cleandocs_shared.audit import AuditWriter  # noqa: E402
from cleandocs_shared.config import Settings  # noqa: E402
from cleandocs_shared.models import TraceContext  # noqa: E402
from cleandocs_shared.pipeline import StageDeps  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402
from cleandocs_shared.utils import doc_pk, processed_key, raw_original_key, s3_uri, sha256_bytes  # noqa: E402
from cleaning_service import main as cleaning  # noqa: E402
from duplicate_service import main as duplicate  # noqa: E402
from embedding_service import main as embedding  # noqa: E402
from extraction_service import main as extraction  # noqa: E402
from manifest_service.logic import ManifestDeps, ingestion_status  # noqa: E402
from metadata_service import main as metadata  # noqa: E402
from normalization_service import main as normalization  # noqa: E402
from reconciliation_service import logic as rlogic  # noqa: E402
from reconciliation_service import main as reconciliation  # noqa: E402
from vector_indexing_service import main as indexing  # noqa: E402

AUTH = {"Authorization": "Bearer s3cret"}
MODS = {"norm": (normalization, "normalization-service"), "clean": (cleaning, "cleaning-service"),
        "dup": (duplicate, "duplicate-service"), "ext": (extraction, "extraction-service"),
        "meta": (metadata, "metadata-service"), "chunk": (chunking, "chunking-service"),
        "emb": (embedding, "embedding-service"), "idx": (indexing, "vector-indexing-service"),
        "rec": (reconciliation, "reconciliation-service")}


class FakeAgent:
    """Lists whatever chunk objects exist in S3 as INDEXED, like a synced Knowledge Base."""

    def __init__(self, s3, bucket):
        self.s3, self.bucket, self.job_status, self.fail_uris = s3, bucket, "COMPLETE", set()

    def start_ingestion_job(self, **kw):
        return {"ingestionJob": {"ingestionJobId": "job1", "status": "STARTING"}}

    def get_ingestion_job(self, **kw):
        return {"ingestionJob": {"ingestionJobId": kw["ingestionJobId"], "status": self.job_status, "statistics": {}}}

    def list_knowledge_base_documents(self, **kw):
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=self.bucket, Prefix="kb-source/").get("Contents", [])
                if o["Key"].endswith(".txt")]
        return {"documentDetails": [{"status": "FAILED" if f"s3://{self.bucket}/{k}" in self.fail_uris else "INDEXED",
                                     "identifier": {"s3": {"uri": f"s3://{self.bucket}/{k}"}}} for k in keys]}


@pytest.fixture
def env(aws_env):
    agent = FakeAgent(aws_env["s3"], aws_env["bucket"])
    clients, reg = {}, Registry(aws_env["registry_table"])
    for key, (mod, name) in MODS.items():
        st = Settings(service_name=name, service_version="0.1.0", s3_bucket=aws_env["bucket"], service_auth_secret=SecretStr("s3cret"),
                      bedrock_knowledge_base_id="KB1", bedrock_data_source_id="DS1", chunk_target_chars=400, chunk_max_chars=600,
                      chunk_overlap_chars=40)
        deps = StageDeps(settings=st, s3=aws_env["s3"], registry=reg, audit=AuditWriter(aws_env["trail_table"], name, "0.1.0"),
                         bedrock_agent=agent)
        clients[key] = TestClient(mod.create_app(st, deps), raise_server_exceptions=False)
        if key == "rec":
            clients["rec_deps"] = deps
    return {"c": clients, "agent": agent, "reg": reg, "aws": aws_env}


def docx_bytes(tag="A"):
    from docx import Document

    d = Document()
    d.add_heading(f"Report {tag}", 1)
    for i in range(10):
        d.add_paragraph(f"Paragraph {i} of {tag}: " + "Steady growth in revenue and margin was observed. " * 3)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def register(env, doc_id, data, ing="ing_1", parent=None, role="FILE", filename=None):
    aws, reg = env["aws"], env["reg"]
    filename = filename or f"{doc_id}.docx"
    key = raw_original_key(doc_id, filename)
    aws["s3"].put_object(Bucket=aws["bucket"], Key=key, Body=data)
    if reg.get_ingestion(ing) is None:
        reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(document_id=doc_id, ingestion_id=ing, original_filename=filename, extension="docx", size_bytes=len(data),
                        checksum_sha256=sha256_bytes(data), s3_uri_original=s3_uri(aws["bucket"], key), parent_document_id=parent)
    reg.update_document(doc_id, stage="VALIDATED", file_kind="docx", is_archive=False)
    reg.add_member(ingestion_id=ing, document_id=doc_id, role=role)


def trail(env, service, doc_id=None, ing="ing_1"):
    ctx = TraceContext(document_id=doc_id, ingestion_id=ing)
    with AuditWriter(env["aws"]["trail_table"], service, "0.1.0").execution(ctx):
        pass


def call(env, name, doc_id=None, ing="ing_1", params=None):
    ctx = {"trace_id": "t", "ingestion_id": ing, **({"document_id": doc_id} if doc_id else {})}
    return env["c"][name].post("/api/v1/process", headers=AUTH, json={"context": ctx, "parameters": params or {}})


def process(env, doc_id, ing="ing_1", through="emb", validated=True):
    if validated:  # the ingestion workflow's validation step, simulated with the same evidence it leaves
        env["aws"]["s3"].put_object(Bucket=env["aws"]["bucket"], Key=processed_key(doc_id, "metadata", "validation.json"), Body=b"{}")
        trail(env, "validation-service", doc_id, ing)
    for n in ("norm", "clean", "dup", "ext", "meta", "chunk", "emb"):
        r = call(env, n, doc_id, ing)
        assert r.status_code == 200, (n, r.text)
        if n == through:
            break


def index(env, ing="ing_1"):
    assert call(env, "idx", None, ing).status_code == 200
    r = env["c"]["idx"].post(f"/api/v1/ingestions/{ing}/index/poll", headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "COMPLETE", r.text


def reconcile(env, ing="ing_1", **params):
    r = call(env, "rec", None, ing, params)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def checks(env, doc_id):
    d = env["reg"].get_document(doc_id)
    return {f["check"]: f for f in d["reconciliation"]["findings"]}, d


@pytest.fixture
def done(env):
    register(env, "d1", docx_bytes("one"))
    process(env, "d1")
    index(env)
    return env


def test_complete_evidence_gives_completed_document_ingestion_and_report(done):
    res = reconcile(done)
    assert res["complete"] and res["finalized"] and res["status"] == "COMPLETED" and res["problem_count"] == 0
    f, d = checks(done, "d1")
    assert d["status"] == "COMPLETED" and d["stage"] == "RECONCILED" and d["reconciliation"]["verdict"] == "COMPLETED"
    assert all(x["ok"] for x in f.values()) and {"original_exists", "chunk_objects", "chunks_indexed", "audit_validation"} <= set(f)
    assert d["graph_status"] == "NOT_CONFIGURED"
    ing = done["reg"].get_ingestion("ing_1")
    assert ing["status"] == "COMPLETED" and ing["reconciliation_status"] == "COMPLETED" and ing["reconciled_at"]
    stored = done["c"]["rec"].get("/api/v1/ingestions/ing_1/reconciliation", headers=AUTH).json()
    assert stored["status"] == "COMPLETED" and stored["documents"][0]["document_id"] == "d1"
    assert done["c"]["rec"].get("/api/v1/documents/d1/reconciliation", headers=AUTH).json()["verdict"] == "COMPLETED"


def test_rerun_checks_nothing_twice_and_force_rechecks(done):
    reconcile(done)
    done["aws"]["trail_table"]  # noqa: B018
    again = call(done, "rec").json()
    assert again["metrics"]["documents_checked"] == 0 and again["result"]["status"] == "COMPLETED"
    forced = call(done, "rec", params={"force": True}).json()
    assert forced["metrics"]["documents_checked"] == 1


def test_missing_chunk_object_is_failed_even_though_registry_said_completed(done):
    keys = [o["Key"] for o in done["aws"]["s3"].list_objects_v2(Bucket=done["aws"]["bucket"], Prefix="kb-source/d1/")["Contents"]]
    done["aws"]["s3"].delete_object(Bucket=done["aws"]["bucket"], Key=[k for k in keys if k.endswith(".txt")][0])
    assert done["reg"].get_document("d1")["status"] == "COMPLETED"
    res = reconcile(done)
    f, d = checks(done, "d1")
    assert not f["chunk_objects"]["ok"] and d["status"] == "FAILED" and d["error_code"] == "RECONCILIATION_EVIDENCE_MISSING"
    assert res["status"] == "FAILED" and res["problems"][0]["document_id"] == "d1"
    assert done["reg"].list_members("ing_1")[0]["status"] == "FAILED"


def test_knowledge_base_not_indexed_downgrades_to_partial(done):
    key = [o["Key"] for o in done["aws"]["s3"].list_objects_v2(Bucket=done["aws"]["bucket"], Prefix="kb-source/d1/")["Contents"]
           if o["Key"].endswith(".txt")][0]
    done["agent"].fail_uris.add(f"s3://{done['aws']['bucket']}/{key}")
    res = reconcile(done)
    f, d = checks(done, "d1")
    assert not f["chunks_indexed"]["ok"] and f["chunk_objects"]["ok"]
    assert d["status"] == "PARTIAL" and res["status"] == "PARTIAL"


def test_missing_audit_record_downgrades_to_partial(done):
    t = done["aws"]["trail_table"]
    for item in t.query(KeyConditionExpression=__import__("boto3").dynamodb.conditions.Key("PK").eq(doc_pk("d1")))["Items"]:
        if item["service_name"] == "cleaning-service":
            t.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    reconcile(done)
    f, d = checks(done, "d1")
    assert not f["audit_cleaning"]["ok"] and f["audit_normalization"]["ok"] and d["status"] == "PARTIAL"


def test_tampered_original_is_failed(done):
    bucket = done["aws"]["bucket"]
    doc = done["reg"].get_document("d1")
    from cleandocs_shared.utils import parse_s3_uri

    key = parse_s3_uri(doc["s3_uri_original"])[1]
    same_size = b"x" * doc["size_bytes"]
    done["aws"]["s3"].put_object(Bucket=bucket, Key=key, Body=same_size)
    reconcile(done)
    f, d = checks(done, "d1")
    assert not f["original_exists"]["ok"] and "SHA-256" in f["original_exists"]["detail"] and d["status"] == "FAILED"


def test_orphan_chunk_object_is_flagged_and_repair_removes_only_orphans(done):
    s3, bucket = done["aws"]["s3"], done["aws"]["bucket"]
    s3.put_object(Bucket=bucket, Key="kb-source/d1/stray.txt", Body=b"left over from an older version")
    before = len(s3.list_objects_v2(Bucket=bucket, Prefix="kb-source/d1/")["Contents"])
    reconcile(done)
    f, d = checks(done, "d1")
    assert not f["orphan_chunk_objects"]["ok"] and not f["stale_kb_entries"]["ok"] and d["status"] == "PARTIAL"
    r = done["c"]["rec"].post("/api/v1/documents/d1/reconcile?repair=true", headers=AUTH)
    findings = {x["check"]: x for x in r.json()["findings"]}
    assert r.status_code == 200 and findings["orphan_chunk_objects"]["ok"]
    assert not findings["stale_kb_entries"]["ok"]            # the KB listing still holds it until the next sync, so not COMPLETED yet
    assert len(s3.list_objects_v2(Bucket=bucket, Prefix="kb-source/d1/")["Contents"]) == before - 1   # only the orphan went
    again = done["c"]["rec"].post("/api/v1/documents/d1/reconcile", headers=AUTH).json()               # the sync has happened
    assert again["verdict"] == "COMPLETED" and done["reg"].get_document("d1")["status"] == "COMPLETED"


def test_chunk_text_integrity_is_verified_on_request(done):
    s3, bucket = done["aws"]["s3"], done["aws"]["bucket"]
    key = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="kb-source/d1/")["Contents"] if o["Key"].endswith(".txt")][0]
    s3.put_object(Bucket=bucket, Key=key, Body=b"edited by hand")
    assert reconcile(done)["status"] == "COMPLETED"          # existence alone cannot see the edit
    res = reconcile(done, force=True, verify_chunk_text=True)
    f, _ = checks(done, "d1")
    assert not f["chunk_text_integrity"]["ok"] and res["status"] == "FAILED"


def test_graph_status_is_corrected_to_what_the_trail_shows_never_completed(done):
    done["reg"].update_document("d1", graph_status="COMPLETED")     # nothing in the trail supports this
    reconcile(done)
    f, d = checks(done, "d1")
    assert d["graph_status"] == "NOT_CONFIGURED" and "corrected" in f["graph_status"]["detail"]
    trail(done, "graph-service", "d1")                                # a graph skeleton call is recorded as COMPLETED here
    done["reg"].update_document("d1", graph_status="NOT_CONFIGURED")
    reconcile(done, force=True)
    assert done["reg"].get_document("d1")["graph_status"] == "NOT_IMPLEMENTED"


def test_duplicates_are_verified_not_upgraded_and_ingestion_still_completes(env):
    data = docx_bytes("same")
    register(env, "d1", data)
    register(env, "d2", data)
    process(env, "d1")
    for n in ("norm", "clean", "dup"):
        assert call(env, n, "d2").status_code == 200
    assert env["reg"].get_document("d2")["status"] == "DUPLICATE"
    index(env)
    res = reconcile(env)
    f, d2 = checks(env, "d2")
    assert d2["status"] == "DUPLICATE" and f["canonical_document"]["ok"] and res["status"] == "COMPLETED"
    assert res["counts"] == {"COMPLETED": 1, "DUPLICATE": 1}


def test_running_documents_stay_processing_and_ingestion_is_not_finalised(env):
    register(env, "d1", docx_bytes("one"))
    process(env, "d1")
    index(env)
    register(env, "d3", docx_bytes("three"))
    process(env, "d3", through="chunk")
    res = reconcile(env)
    assert res["complete"] and not res["finalized"] and res["status"] == "PROCESSING"
    assert env["reg"].get_document("d3")["status"] == "PROCESSING"
    assert "reconciled_at" not in env["reg"].get_ingestion("ing_1")


def test_time_budget_returns_partial_progress_and_resumes(env, monkeypatch):
    for d in ("d1", "d2"):
        register(env, d, docx_bytes(d))
        process(env, d)
    index(env)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(rlogic.time, "monotonic", lambda: next(clock))
    first = reconcile(env)
    assert first == {"complete": False, "documents_checked": 1, "documents_remaining": 1}
    second = reconcile(env)
    assert second["complete"] and second["finalized"] and second["status"] == "COMPLETED"


def test_archive_children_must_all_be_accounted_for(env):
    register(env, "zip1", b"PK-archive", role="ARCHIVE", filename="bundle.zip")
    register(env, "c1", docx_bytes("child"), parent="zip1")
    process(env, "c1", validated=False)
    trail(env, "archive-service", "zip1")
    env["aws"]["s3"].put_object(
        Bucket=env["aws"]["bucket"], Key=processed_key("zip1", "manifests", "archive_manifest.json"),
        Body=json.dumps({"parent_document_id": "zip1", "entries": [],   # the shape archive-service really writes
                         "summary": {"children": [{"document_id": "c1"}, {"document_id": "ghost"}]}}).encode())
    index(env)
    res = reconcile(env)
    f, c1 = checks(env, "c1")
    assert f["audit_archive_parent"]["ok"] and c1["status"] == "COMPLETED"
    assert res["status"] == "PARTIAL"                                    # a file the archive produced is not tracked
    assert "archive_children:zip1" in [x["check"] for x in env["c"]["rec"].get("/api/v1/ingestions/ing_1/reconciliation", headers=AUTH).json()["ingestion_checks"] if not x["ok"]]
    assert env["reg"].get_document("zip1")["status"] == "COMPLETED"


def test_manifest_status_poll_does_not_overwrite_a_reconciled_ingestion(done):
    done["agent"].fail_uris.add(next(u for u in [f"s3://{done['aws']['bucket']}/{o['Key']}" for o in done["aws"]["s3"].list_objects_v2(
        Bucket=done["aws"]["bucket"], Prefix="kb-source/d1/")["Contents"]] if u.endswith(".txt")))
    reconcile(done)
    deps = ManifestDeps(settings=done["c"]["rec_deps"].settings, registry=done["reg"], audit=done["c"]["rec_deps"].audit)
    ingestion_status(deps, "ing_1")
    assert done["reg"].get_ingestion("ing_1")["status"] == "PARTIAL"


def test_request_validation_auth_and_configuration(env):
    assert call(env, "rec", params={"unknown": 1}).status_code == 422
    assert call(env, "rec", params={"max_seconds": 1}).status_code == 422
    assert env["c"]["rec"].post("/api/v1/process", json={"context": {"ingestion_id": "x"}}).status_code == 401
    assert call(env, "rec", ing="missing").status_code == 409 or call(env, "rec", ing="missing").status_code == 404
    assert env["c"]["rec"].get("/api/v1/ingestions/none/reconciliation", headers=AUTH).status_code in (404, 409)
    no_kb = Settings(service_name="reconciliation-service", service_version="0.1.0", s3_bucket=env["aws"]["bucket"],
                     service_auth_secret=SecretStr("s3cret"))
    register(env, "d1", docx_bytes("one"))
    deps = StageDeps(settings=no_kb, s3=env["aws"]["s3"], registry=env["reg"],
                     audit=AuditWriter(env["aws"]["trail_table"], "reconciliation-service", "0.1.0"), bedrock_agent=env["agent"])
    r = TestClient(reconciliation.create_app(no_kb, deps), raise_server_exceptions=False).post(
        "/api/v1/process", headers=AUTH, json={"context": {"ingestion_id": "ing_1", "trace_id": "t"}})
    assert r.status_code == 422 and r.json()["error_code"] == "KB_NOT_CONFIGURED"
