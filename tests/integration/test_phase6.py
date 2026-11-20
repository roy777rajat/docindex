"""entity-extraction (non-blocking candidates) and graph-service (NOT_IMPLEMENTED contract) against mocked AWS."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

ROOT = Path(__file__).resolve().parents[2]
for s in ("entity-extraction", "graph"):
    sys.path.insert(0, str(ROOT / "services" / f"{s}-service"))

from cleandocs_shared.audit import AuditWriter  # noqa: E402
from cleandocs_shared.config import Settings  # noqa: E402
from cleandocs_shared.graph_models import Entity, EvidenceRef, Relationship  # noqa: E402
from cleandocs_shared.pipeline import StageDeps  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402
from entity_extraction_service import main as entities  # noqa: E402
from graph_service import main as graph  # noqa: E402

AUTH = {"Authorization": "Bearer s3cret"}
TEXT = ("Invoice No: INV-2025-001 issued by Acme Traders Pvt Ltd to Dr. Meera Rao on 2025-01-31. "
        "Amount due ₹4,500.00 (₹4,500.00 again). Contact billing@acme.example. Reference: pending. See Acme Traders Pvt Ltd.")


@pytest.fixture
def env(aws_env):
    def client(mod, name):
        st = Settings(service_name=name, service_version="0.1.0", s3_bucket=aws_env["bucket"], service_auth_secret=SecretStr("s3cret"))
        d = StageDeps(settings=st, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
                      audit=AuditWriter(aws_env["trail_table"], name, "0.1.0"))
        return TestClient(mod.create_app(st, d), raise_server_exceptions=False)

    reg = Registry(aws_env["registry_table"])
    reg.create_ingestion(ingestion_id="ing_1", trace_id="t")
    reg.create_document(document_id="d1", ingestion_id="ing_1", original_filename="inv.pdf", extension="pdf", size_bytes=1,
                        checksum_sha256="a" * 64, s3_uri_original="s3://b/x", archive_entry_path="2025/inv.pdf", parent_document_id="z")
    reg.add_member(ingestion_id="ing_1", document_id="d1")
    ext = {"document_id": "d1", "data": {"document": {"kind": "pdf"}, "pages": [
        {"page": 2, "blocks": [{"id": "p2-b1", "type": "paragraph", "text": TEXT, "section": "Billing"}]}]}}
    chunks = {"data": {"chunks": [{"chunk_id": "c0000-abc", "block_ids": ["p2-b1"]}]}}
    for k, v in (("extraction/extraction.json", ext), ("chunks/chunks.json", chunks)):
        aws_env["s3"].put_object(Bucket=aws_env["bucket"], Key=f"processed/documents/d1/{k}", Body=json.dumps(v).encode())
    return {"ent": client(entities, "entity-extraction-service"), "graph": client(graph, "graph-service"), "reg": reg, "aws": aws_env}


def post(c, doc="d1", path="/api/v1/process"):
    return c.post(path, headers=AUTH, json={"context": {"document_id": doc, "trace_id": "t"}})


def cands(env):
    return json.loads(env["aws"]["s3"].get_object(Bucket=env["aws"]["bucket"], Key="processed/documents/d1/graph-candidates/entities.json")["Body"].read())["data"]["entities"]


def test_candidates_have_types_evidence_and_citation_locators(env):
    r = post(env["ent"]).json()
    assert r["status"] == "COMPLETED" and r["result"]["by_type"] == {"DATE": 1, "EMAIL": 1, "IDENTIFIER": 1, "MONEY": 1, "ORGANIZATION": 1, "PERSON": 1}
    by = {e["type"]: e for e in cands(env)}
    assert by["MONEY"]["normalized"] == "₹4500.00" and len(by["MONEY"]["evidence"]) == 2  # same value grouped, evidence kept
    ev = by["PERSON"]["evidence"][0]
    assert by["PERSON"]["text"] == "Meera Rao" and ev["page"] == 2 and ev["chunk_ids"] == ["c0000-abc"] and ev["section"] == "Billing"
    assert ev["archive_entry_path"] == "2025/inv.pdf" and TEXT[ev["char_start"]:ev["char_end"]] == "Meera Rao"
    assert by["IDENTIFIER"]["text"] == "INV-2025-001"  # "Reference: pending" has no digit, so it is not an identifier
    assert by["ORGANIZATION"]["evidence"][0]["snippet"] and by["EMAIL"]["confidence"] == 0.95
    assert len(by["ORGANIZATION"]["evidence"]) == 2
    for e in cands(env):
        Entity.model_validate(e)  # output honours the published schema
    assert env["reg"].get_document("d1")["entity_candidate_count"] == 6


def test_entity_stage_is_idempotent_and_never_fails_the_document(env):
    post(env["ent"])
    assert post(env["ent"]).json()["status"] == "SKIPPED"
    assert env["reg"].get_document("d1")["stage"] == "UPLOADED"  # optional branch does not move the main stage
    assert post(env["ent"], "nope").status_code == 404
    env["reg"].create_document(document_id="d2", ingestion_id="ing_1", original_filename="b", extension="pdf", size_bytes=1,
                               checksum_sha256="b" * 64, s3_uri_original="s3://b/y")
    assert post(env["ent"], "d2").json()["error_code"] == "PREVIOUS_STAGE_OUTPUT_MISSING"


def test_non_blocking_failure_leaves_document_status_untouched(env, monkeypatch):
    import entity_extraction_service.logic as lg
    from cleandocs_shared.exceptions import PermanentBusinessFailure

    def boom(*a, **k):
        raise PermanentBusinessFailure("bad", code="X")

    monkeypatch.setattr(lg, "Entity", boom)
    assert post(env["ent"]).status_code == 409
    assert env["reg"].get_document("d1")["status"] == "PROCESSING"
    assert env["reg"].list_members("ing_1")[0]["status"] == "PROCESSING"


def test_graph_process_records_not_implemented_and_does_not_block(env):
    r = post(env["graph"])
    assert r.status_code == 200 and r.json()["status"] == "NOT_IMPLEMENTED"
    doc = env["reg"].get_document("d1")
    assert doc["graph_status"] == "NOT_IMPLEMENTED" and doc["status"] == "PROCESSING"
    rows = [x for x in env["aws"]["trail_table"].scan()["Items"] if x["service_name"] == "graph-service" and x["status"] == "NOT_IMPLEMENTED"]
    assert rows


@pytest.mark.parametrize("op", ["resolve-entities", "extract-relationships", "persist", "validate"])
def test_graph_contract_endpoints_return_501_and_audit(env, op):
    r = env["graph"].post(f"/api/v1/graph/{op}", headers=AUTH, json={"document_id": "d1"})
    assert r.status_code == 501 and r.json() == {"status": "NOT_IMPLEMENTED", "operation": op,
                                                  "message": "the knowledge graph is not implemented in this release"}
    assert env["graph"].post(f"/api/v1/graph/{op}", json={"document_id": "d1"}).status_code == 401
    assert env["graph"].post(f"/api/v1/graph/{op}", headers=AUTH, json={}).status_code == 422
    rows = [x for x in env["aws"]["trail_table"].scan()["Items"] if x["service_name"] == "graph-service" and x["status"] == "NOT_IMPLEMENTED"]
    assert any(x.get("metrics", {}).get("operation") == op for x in rows)


def test_graph_status_is_never_completed(env):
    post(env["graph"])
    env["graph"].post("/api/v1/graph/persist", headers=AUTH, json={"document_id": "d1"})
    assert env["reg"].get_document("d1")["graph_status"] != "COMPLETED"


def test_schemas_reject_unevidenced_or_out_of_range_data():
    ev = EvidenceRef(document_id="d")
    with pytest.raises(ValidationError):
        Entity(entity_id="e", type="PERSON", text="x", normalized="x", confidence=1.5, method="regex", evidence=[ev])
    with pytest.raises(ValidationError):
        Entity(entity_id="e", type="PERSON", text="x", normalized="x", confidence=0.5, method="regex", evidence=[])
    with pytest.raises(ValidationError):
        Relationship(relationship_id="r", type="BOGUS", source_entity_id="a", target_entity_id="b", confidence=0.5, method="regex", evidence=[ev])
    with pytest.raises(ValidationError):
        EvidenceRef(document_id="d", page=0)
