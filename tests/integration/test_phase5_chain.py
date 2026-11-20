"""Phase 4 output -> chunking -> embedding-prep -> vector-indexing against mocked AWS and a fake Bedrock agent."""
import io
import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
for s in ("normalization", "cleaning", "duplicate", "extraction", "metadata", "chunking", "embedding", "vector-indexing"):
    sys.path.insert(0, str(ROOT / "services" / f"{s}-service"))

from chunking_service import main as chunking  # noqa: E402
from cleandocs_shared.audit import AuditWriter  # noqa: E402
from cleandocs_shared.config import Settings  # noqa: E402
from cleandocs_shared.pipeline import StageDeps  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402
from cleandocs_shared.utils import raw_original_key, s3_uri, sha256_bytes  # noqa: E402
from cleaning_service import main as cleaning  # noqa: E402
from embedding_service import main as embedding  # noqa: E402
from extraction_service import main as extraction  # noqa: E402
from metadata_service import main as metadata  # noqa: E402
from normalization_service import main as normalization  # noqa: E402
from vector_indexing_service import main as indexing  # noqa: E402

AUTH = {"Authorization": "Bearer s3cret"}
MODS = {"norm": (normalization, "normalization-service"), "clean": (cleaning, "cleaning-service"),
        "ext": (extraction, "extraction-service"), "meta": (metadata, "metadata-service"),
        "chunk": (chunking, "chunking-service"), "emb": (embedding, "embedding-service"),
        "idx": (indexing, "vector-indexing-service")}


class FakeAgent:
    def __init__(self, s3, bucket):
        self.s3, self.bucket = s3, bucket
        self.job_status, self.fail_uris, self.conflict, self.starts = "STARTING", set(), False, 0

    def start_ingestion_job(self, **kw):
        if self.conflict:
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "running"}}, "StartIngestionJob")
        self.starts += 1
        return {"ingestionJob": {"ingestionJobId": f"job{self.starts}", "status": "STARTING"}}

    def get_ingestion_job(self, **kw):
        return {"ingestionJob": {"ingestionJobId": kw["ingestionJobId"], "status": self.job_status,
                                 "failureReasons": ["boom"] if self.job_status == "FAILED" else [],
                                 "statistics": {"numberOfNewDocumentsIndexed": 3}}}

    def list_knowledge_base_documents(self, **kw):
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=self.bucket, Prefix="kb-source/").get("Contents", [])
                if o["Key"].endswith(".txt")]
        docs = [{"status": "FAILED" if f"s3://{self.bucket}/{k}" in self.fail_uris else "INDEXED",
                 "identifier": {"s3": {"uri": f"s3://{self.bucket}/{k}"}}} for k in keys]
        return {"documentDetails": docs}


@pytest.fixture
def env(aws_env):
    agent = FakeAgent(aws_env["s3"], aws_env["bucket"])
    clients = {}
    for key, (mod, name) in MODS.items():
        settings = Settings(service_name=name, service_version="0.1.0", s3_bucket=aws_env["bucket"],
                            service_auth_secret=SecretStr("s3cret"), bedrock_knowledge_base_id="KB1",
                            bedrock_data_source_id="DS1", chunk_target_chars=400, chunk_max_chars=600, chunk_overlap_chars=40)
        deps = StageDeps(settings=settings, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
                         audit=AuditWriter(aws_env["trail_table"], name, "0.1.0"), bedrock_agent=agent)
        clients[key] = TestClient(mod.create_app(settings, deps), raise_server_exceptions=False)
    no_kb = Settings(service_name="vector-indexing-service", service_version="0.1.0", s3_bucket=aws_env["bucket"],
                     service_auth_secret=SecretStr("s3cret"))
    deps = StageDeps(settings=no_kb, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
                     audit=AuditWriter(aws_env["trail_table"], "vector-indexing-service", "0.1.0"), bedrock_agent=agent)
    clients["idx_nokb"] = TestClient(indexing.create_app(no_kb, deps), raise_server_exceptions=False)
    return {"c": clients, "agent": agent, "reg": Registry(aws_env["registry_table"]), "aws": aws_env}


def docx_bytes(paragraphs=14):
    from docx import Document

    d = Document()
    d.add_heading("Annual Report", 1)
    for i in range(paragraphs):
        d.add_paragraph(f"Paragraph {i}: " + "The quarterly findings show steady growth in revenue and margin. " * 3)
    d.add_heading("Conclusion", 2)
    d.add_paragraph("Recommendations follow from the methodology above. Total 1,250.75.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def pdf_bytes(pages=3):
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    for p in range(pages):
        c.drawString(72, 750, f"Page {p + 1} content about invoices and amounts due on schedule today.")
        c.showPage()
    c.save()
    return b.getvalue()


def register(env, doc_id, filename, data, kind, ext, ing="ing_1", entry=None, parent=None):
    aws, reg = env["aws"], env["reg"]
    key = raw_original_key(doc_id, filename)
    aws["s3"].put_object(Bucket=aws["bucket"], Key=key, Body=data)
    if reg.get_ingestion(ing) is None:
        reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(document_id=doc_id, ingestion_id=ing, original_filename=filename, extension=ext, size_bytes=len(data),
                        checksum_sha256=sha256_bytes(data), s3_uri_original=s3_uri(aws["bucket"], key),
                        archive_entry_path=entry, parent_document_id=parent)
    reg.update_document(doc_id, stage="VALIDATED", file_kind=kind, is_archive=False)
    reg.add_member(ingestion_id=ing, document_id=doc_id, role="FILE")


def call(env, name, doc_id=None, ing="ing_1"):
    ctx = {"trace_id": "t", "ingestion_id": ing}
    if doc_id:
        ctx["document_id"] = doc_id
    return env["c"][name].post("/api/v1/process", headers=AUTH, json={"context": ctx})


def prepare(env, doc_id):
    for n in ("norm", "clean", "ext", "meta", "chunk", "emb"):
        r = call(env, n, doc_id)
        assert r.status_code == 200, (n, r.text)


def s3json(env, key):
    return json.loads(env["aws"]["s3"].get_object(Bucket=env["aws"]["bucket"], Key=key)["Body"].read())


def keys(env, prefix):
    return [o["Key"] for o in env["aws"]["s3"].list_objects_v2(Bucket=env["aws"]["bucket"], Prefix=prefix).get("Contents", [])]


def test_docx_is_chunked_with_sections_and_published_with_citation_sidecars(env):
    register(env, "d1", "report.docx", docx_bytes(), "docx", "docx", entry="reports/2025/report.docx", parent="doc_zip")
    prepare(env, "d1")
    doc = env["reg"].get_document("d1")
    assert doc["stage"] == "INDEX_READY" and doc["indexing_state"] == "PENDING" and doc["chunk_count"] > 3
    manifest = s3json(env, "processed/documents/d1/manifests/chunks.json")["data"]
    assert manifest["chunk_count"] == doc["chunk_count"]
    txts = [k for k in keys(env, "kb-source/d1/") if k.endswith(".txt")]
    sidecars = [k for k in keys(env, "kb-source/d1/") if k.endswith(".txt.metadata.json")]
    assert len(txts) == len(sidecars) == manifest["chunk_count"]
    first = s3json(env, sidecars[0])["metadataAttributes"]
    assert len(json.dumps({"metadataAttributes": first}).encode()) <= 1500
    assert first["document_id"] == "d1" and first["filename"] == "report.docx"
    assert first["archive_entry_path"] == "reports/2025/report.docx" and first["parent_document_id"] == "doc_zip"
    assert first["section"].startswith("Annual Report")
    last = s3json(env, sorted(sidecars)[-1])["metadataAttributes"]
    assert last["section"].startswith("Annual Report")  # section = where the chunk starts
    body = env["aws"]["s3"].get_object(Bucket=env["aws"]["bucket"], Key=txts[0])["Body"].read().decode()
    assert body in {c["text"] for c in s3json(env, "processed/documents/d1/chunks/chunks.json")["data"]["chunks"]}
    assert all("text" not in e for e in manifest["chunks"])  # manifest holds pointers and hashes only


def test_pdf_chunks_carry_page_numbers(env):
    register(env, "p1", "inv.pdf", pdf_bytes(3), "pdf", "pdf")
    prepare(env, "p1")
    ch = s3json(env, "processed/documents/p1/chunks/chunks.json")["data"]["chunks"]
    assert ch[0]["page_start"] == 1 and ch[-1]["page_end"] == 3
    attrs = [s3json(env, k)["metadataAttributes"] for k in keys(env, "kb-source/p1/") if k.endswith("metadata.json")]
    assert all("page_start" in a and "page_end" in a and "archive_entry_path" not in a for a in attrs)  # no invented locators


def test_embedding_is_idempotent_and_removes_stale_chunks(env):
    register(env, "d2", "r.docx", docx_bytes(6), "docx", "docx")
    prepare(env, "d2")
    env["aws"]["s3"].put_object(Bucket=env["aws"]["bucket"], Key="kb-source/d2/c9999-stale.txt", Body=b"old content")
    assert call(env, "emb", "d2").json()["status"] == "SKIPPED"  # unchanged input: no rewrite
    assert "kb-source/d2/c9999-stale.txt" in keys(env, "kb-source/d2/")
    # changed content -> new chunk ids -> old objects are removed, not left for the next KB sync
    env["reg"].update_document("d2", checksum_sha256=sha256_bytes(docx_bytes(7)))
    register(env, "d2b", "r2.docx", docx_bytes(7), "docx", "docx")
    prepare(env, "d2b")
    assert keys(env, "kb-source/d2b/")  # separate document unaffected
    # simulate re-chunk of d2 with new extraction content
    ext = s3json(env, "processed/documents/d2/chunks/chunks.json")
    ext["data"]["chunks"][0]["text"] += " edited"
    ext["data"]["chunks"][0]["chunk_id"] = "c0000-newhash0"
    env["aws"]["s3"].put_object(Bucket=env["aws"]["bucket"], Key="processed/documents/d2/chunks/chunks.json", Body=json.dumps(ext).encode())
    r = call(env, "emb", "d2").json()
    assert r["status"] == "COMPLETED" and r["result"]["stale_objects_removed"] >= 3
    assert "kb-source/d2/c9999-stale.txt" not in keys(env, "kb-source/d2/")


def test_embedding_and_chunking_preconditions(env):
    register(env, "x", "r.docx", docx_bytes(2), "docx", "docx")
    assert call(env, "chunk", "x").json()["error_code"] == "PREVIOUS_STAGE_OUTPUT_MISSING"
    assert call(env, "emb", "x").json()["error_code"] == "PREVIOUS_STAGE_OUTPUT_MISSING"
    assert call(env, "chunk", "nope").status_code == 404
    assert env["c"]["chunk"].post("/api/v1/process", json={"context": {"document_id": "x"}}).status_code == 401


def test_indexing_starts_one_job_for_the_batch_and_is_idempotent(env):
    for d in ("a", "b"):
        register(env, d, f"{d}.docx", docx_bytes(3 if d == "a" else 4), "docx", "docx")
        prepare(env, d)
    r = call(env, "idx").json()
    assert r["status"] == "COMPLETED" and r["result"] == {"job_id": "job1", "state": "RUNNING", "documents": 2}
    again = call(env, "idx").json()
    assert again["status"] == "SKIPPED" and again["result"]["job_id"] == "job1" and env["agent"].starts == 1
    assert env["reg"].get_document("a")["indexing_state"] == "INDEXING"


def test_poll_running_then_complete_marks_documents_indexed(env):
    register(env, "a", "a.docx", docx_bytes(3), "docx", "docx")
    prepare(env, "a")
    call(env, "idx")
    poll = lambda: env["c"]["idx"].post("/api/v1/ingestions/ing_1/index/poll", headers=AUTH).json()  # noqa: E731
    env["agent"].job_status = "IN_PROGRESS"
    assert poll()["state"] == "RUNNING"
    env["agent"].job_status = "COMPLETE"
    r = poll()
    assert r["state"] == "COMPLETE" and r["documents_indexed"] == 1 and r["documents_failed"] == 0
    doc = env["reg"].get_document("a")
    assert doc["stage"] == "INDEXED" and doc["status"] == "COMPLETED" and doc["kb_chunk_count"] == doc["chunk_count"]
    assert env["reg"].list_members("ing_1")[0]["status"] == "COMPLETED"
    assert poll()["documents_indexed"] == 0  # already finalized: nothing re-processed


def test_poll_fails_document_when_any_chunk_is_not_indexed(env):
    register(env, "a", "a.docx", docx_bytes(3), "docx", "docx")
    register(env, "b", "b.docx", docx_bytes(4), "docx", "docx")
    prepare(env, "a"); prepare(env, "b")
    call(env, "idx")
    env["agent"].fail_uris = {f"s3://{env['aws']['bucket']}/{keys(env, 'kb-source/b/')[0]}".replace(".metadata.json", "")}
    env["agent"].job_status = "COMPLETE"
    r = env["c"]["idx"].post("/api/v1/ingestions/ing_1/index/poll", headers=AUTH).json()
    assert (r["documents_indexed"], r["documents_failed"]) == (1, 1)
    assert env["reg"].get_document("b")["error_code"] == "CHUNKS_NOT_INDEXED" and env["reg"].get_document("b")["status"] == "FAILED"
    assert env["reg"].get_document("a")["status"] == "COMPLETED"


def test_poll_job_failure_fails_waiting_documents(env):
    register(env, "a", "a.docx", docx_bytes(3), "docx", "docx")
    prepare(env, "a")
    call(env, "idx")
    env["agent"].job_status = "FAILED"
    r = env["c"]["idx"].post("/api/v1/ingestions/ing_1/index/poll", headers=AUTH).json()
    assert r["state"] == "FAILED" and r["documents_failed"] == 1
    assert env["reg"].get_document("a")["error_code"] == "KB_JOB_FAILED"


def test_indexing_edge_cases(env):
    assert call(env, "idx", ing="missing").status_code == 404
    env["reg"].create_ingestion(ingestion_id="empty", trace_id="t")
    r = call(env, "idx", ing="empty").json()
    assert r["status"] == "SKIPPED" and r["result"]["state"] == "NOTHING_TO_INDEX" and env["agent"].starts == 0
    register(env, "a", "a.docx", docx_bytes(3), "docx", "docx")
    prepare(env, "a")
    env["agent"].conflict = True
    assert call(env, "idx").status_code == 503
    assert call(env, "idx_nokb").json()["error_code"] == "KB_NOT_CONFIGURED"
    assert env["c"]["idx"].post("/api/v1/ingestions/ing_1/index/poll", headers=AUTH).status_code == 404  # no job yet
