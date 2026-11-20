import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.registry import Registry
from manifest_service.logic import ManifestDeps
from manifest_service.main import SERVICE_NAME, VERSION, create_app

AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def env(aws_env):
    settings = Settings(service_name=SERVICE_NAME, service_version=VERSION, s3_bucket=aws_env["bucket"], service_auth_secret=SecretStr("s3cret"))
    reg = Registry(aws_env["registry_table"])
    deps = ManifestDeps(settings=settings, registry=reg, audit=AuditWriter(aws_env["trail_table"], SERVICE_NAME, VERSION))
    return TestClient(create_app(settings, deps), raise_server_exceptions=False), reg


def add(reg, doc, role="FILE", status="PROCESSING", ing="ing_1"):
    if reg.get_ingestion(ing) is None:
        reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(document_id=doc, ingestion_id=ing, original_filename=f"{doc}.pdf", extension="pdf", size_bytes=1,
                        checksum_sha256="a" * 64, s3_uri_original=f"s3://b/{doc}")
    reg.add_member(ingestion_id=ing, document_id=doc, role=role, status=status)


def post_state(c, doc, status, ing="ing_1"):
    return c.post(f"/api/v1/ingestions/{ing}/documents/{doc}/state", headers=AUTH, json={"status": status})


def test_process_lists_only_processing_files_not_archives(env):
    c, reg = env
    add(reg, "z", role="ARCHIVE")
    add(reg, "a"); add(reg, "b"); add(reg, "r", status="REJECTED")
    r = c.post("/api/v1/process", headers=AUTH, json={"context": {"ingestion_id": "ing_1"}}).json()
    assert [f["document_id"] for f in r["result"]["files"]] == ["a", "b"]
    assert r["result"]["total_files"] == 3 and r["result"]["status"] == "PROCESSING"


def test_rollup_completed_partial_failed(env):
    c, reg = env
    for d in "abc":
        add(reg, d)
    assert c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()["all_terminal"] is False
    post_state(c, "a", "COMPLETED"); post_state(c, "b", "DUPLICATE"); post_state(c, "c", "FAILED")
    s = c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()
    assert s["all_terminal"] and s["status"] == "PARTIAL"
    assert reg.get_ingestion("ing_1")["status"] == "PARTIAL"


def test_archive_status_follows_children_and_single_file(env):
    c, reg = env
    add(reg, "z", role="ARCHIVE"); add(reg, "a"); add(reg, "b")
    post_state(c, "a", "COMPLETED"); post_state(c, "b", "COMPLETED")
    assert c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()["status"] == "COMPLETED"
    assert reg.get_document("z")["status"] == "COMPLETED"
    add(reg, "s", ing="ing_2")
    post_state(c, "s", "REJECTED", ing="ing_2")
    assert c.get("/api/v1/ingestions/ing_2", headers=AUTH).json()["status"] == "FAILED"


def test_state_validation_and_conflicts(env):
    c, reg = env
    add(reg, "a")
    assert post_state(c, "a", "BOGUS").status_code == 422
    assert post_state(c, "nope", "COMPLETED").status_code == 404
    assert c.get("/api/v1/ingestions/missing", headers=AUTH).status_code == 404
    assert post_state(c, "a", "COMPLETED").status_code == 200
    assert post_state(c, "a", "COMPLETED").status_code == 200  # idempotent
    assert post_state(c, "a", "FAILED").status_code == 422     # no terminal rewrite
    assert c.get("/api/v1/ingestions/ing_1").status_code == 401


def test_settled_means_every_file_terminal_or_index_ready(env):
    c, reg = env
    for d in "abc":
        add(reg, d)
    s = c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()
    assert s["all_settled"] is False and s["pending_files"] == 3
    reg.update_document("a", stage="INDEX_READY", indexing_state="PENDING")
    post_state(c, "b", "DUPLICATE")
    reg.update_document("c", stage="EXTRACTED")
    s = c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()
    assert (s["index_ready_files"], s["pending_files"], s["all_settled"]) == (1, 1, False)
    reg.update_document("c", stage="INDEX_READY", indexing_state="PENDING")
    s = c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()
    assert (s["index_ready_files"], s["all_settled"], s["status"]) == (2, True, "PROCESSING")
    reg.update_document("a", indexing_state="INDEXING")  # already handed to the knowledge base job
    assert c.get("/api/v1/ingestions/ing_1", headers=AUTH).json()["index_ready_files"] == 1
