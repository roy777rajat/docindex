"""Runs a document through normalization -> cleaning -> duplicate -> extraction -> metadata against mocked AWS."""
import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
for s in ("normalization", "cleaning", "duplicate", "extraction", "metadata"):
    sys.path.insert(0, str(ROOT / "services" / f"{s}-service"))

from cleandocs_shared.audit import AuditWriter  # noqa: E402
from cleandocs_shared.config import Settings  # noqa: E402
from cleandocs_shared.pipeline import StageDeps  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402
from cleandocs_shared.utils import raw_original_key, s3_uri, sha256_bytes  # noqa: E402
from cleaning_service import main as cleaning  # noqa: E402
from duplicate_service import main as duplicate  # noqa: E402
from extraction_service import main as extraction  # noqa: E402
from metadata_service import main as metadata  # noqa: E402
from normalization_service import main as normalization  # noqa: E402

AUTH = {"Authorization": "Bearer s3cret"}


class FakeTextract:
    def __init__(self):
        self.calls = 0

    def detect_document_text(self, Document):
        self.calls += 1
        return {"Blocks": [{"BlockType": "LINE", "Text": "Scanned  invoice   total 4,500", "Confidence": 99.0},
                           {"BlockType": "LINE", "Text": "Due 2025-01-31", "Confidence": 97.0},
                           {"BlockType": "WORD", "Text": "x", "Confidence": 50.0}]}


@pytest.fixture
def env(aws_env):
    tx = FakeTextract()
    clients = {}

    def client(mod, name, **kw):
        settings = Settings(service_name=name, service_version="0.1.0", s3_bucket=aws_env["bucket"],
                            service_auth_secret=SecretStr("s3cret"), **kw)
        deps = StageDeps(settings=settings, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
                         audit=AuditWriter(aws_env["trail_table"], name, "0.1.0"), textract=tx)
        return TestClient(mod.create_app(settings, deps), raise_server_exceptions=False)

    clients["norm"] = client(normalization, "normalization-service")
    clients["clean"] = client(cleaning, "cleaning-service")
    clients["dup"] = client(duplicate, "duplicate-service")
    clients["ext"] = client(extraction, "extraction-service")
    clients["ext_off"] = client(extraction, "extraction-service", ocr_enabled=False)
    clients["ext_cap"] = client(extraction, "extraction-service", textract_monthly_page_cap=0)
    clients["meta"] = client(metadata, "metadata-service")
    return {"c": clients, "tx": tx, "reg": Registry(aws_env["registry_table"]), "aws": aws_env}


def register(env, doc_id, filename, data, kind, ext, ing="ing_1"):
    aws, reg = env["aws"], env["reg"]
    key = raw_original_key(doc_id, filename)
    aws["s3"].put_object(Bucket=aws["bucket"], Key=key, Body=data)
    if reg.get_ingestion(ing) is None:
        reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(document_id=doc_id, ingestion_id=ing, original_filename=filename, extension=ext, size_bytes=len(data),
                        checksum_sha256=sha256_bytes(data), s3_uri_original=s3_uri(aws["bucket"], key),
                        parent_document_id="doc_zip" if filename.startswith("in_zip") else None)
    reg.update_document(doc_id, stage="VALIDATED", file_kind=kind, is_archive=False)
    reg.add_member(ingestion_id=ing, document_id=doc_id, role="FILE")


def call(env, name, doc_id, **params):
    r = env["c"][name].post("/api/v1/process", headers=AUTH, json={"context": {"document_id": doc_id, "trace_id": "t"}, "parameters": params})
    return r


def pipeline(env, doc_id):
    out = {}
    for name in ("norm", "clean", "dup", "ext", "meta"):
        r = call(env, name, doc_id)
        assert r.status_code == 200, (name, r.text)
        out[name] = r.json()
    return out


def make_docx():
    from docx import Document

    d = Document()
    d.add_heading("Quarterly Report", 1)
    d.add_paragraph("Executive summary: revenue was 1,250.75 in 2025-03-31.  Findings are strong.")
    d.add_heading("Details", 2)
    d.add_paragraph("Methodology and conclusion with recommendations.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Item", "Amount", "A", "42"
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_pdf(text="Invoice number 1001. Bill to ACME. Amount due 4,500 on 2025-01-31. GST included. Subtotal shown."):
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.drawString(72, 750, text)
    c.showPage()
    c.drawString(72, 750, "Second page has more words to read here for the extractor.")
    c.save()
    return b.getvalue()


def make_blank_pdf():
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.rect(50, 50, 100, 100)
    c.showPage()
    c.save()
    return b.getvalue()


def stored(env, doc_id, stage, name):
    return json.loads(env["aws"]["s3"].get_object(Bucket=env["aws"]["bucket"], Key=f"processed/documents/{doc_id}/{stage}/{name}")["Body"].read())


def test_docx_keeps_headings_sections_and_tables(env):
    register(env, "d1", "r.docx", make_docx(), "docx", "docx")
    out = pipeline(env, "d1")
    ext = stored(env, "d1", "extraction", "extraction.json")["data"]
    blocks = ext["pages"][0]["blocks"]
    assert [b["type"] for b in blocks] == ["heading", "paragraph", "heading", "paragraph", "table"]
    assert blocks[3]["section"] == "Quarterly Report > Details" and "Item | Amount" in blocks[4]["text"]
    assert ext["document"]["filename"] == "r.docx"
    meta = out["meta"]["result"]
    assert meta["title"] == "Quarterly Report" and meta["doc_type"] == "report" and "2025-03-31" in meta["dates_mentioned"]
    assert env["reg"].get_document("d1")["stage"] == "CLASSIFIED"
    assert "1,250.75" in json.dumps(ext)  # numbers survive cleaning


def test_searchable_pdf_uses_text_layer_with_page_locators_and_no_ocr(env):
    register(env, "d2", "inv.pdf", make_pdf(), "pdf", "pdf")
    out = pipeline(env, "d2")
    assert out["norm"]["result"]["needs_ocr"] is False and env["tx"].calls == 0
    pages = stored(env, "d2", "extraction", "extraction.json")["data"]["pages"]
    assert [p["page"] for p in pages] == [1, 2] and pages[0]["method"] == "text_layer"
    assert out["meta"]["result"]["doc_type"] == "invoice" and out["meta"]["result"]["page_count"] == 2


def test_scanned_pdf_goes_through_textract_and_budget(env):
    register(env, "d3", "scan.pdf", make_blank_pdf(), "pdf", "pdf")
    out = pipeline(env, "d3")
    assert out["norm"]["result"]["ocr_pages_needed"] == 1 and env["tx"].calls == 1
    page = stored(env, "d3", "extraction", "extraction.json")["data"]["pages"][0]
    assert page["method"] == "textract" and page["ocr_confidence"] == 98.0
    assert page["blocks"][0]["text"] == "Scanned invoice total 4,500\nDue 2025-01-31"
    assert env["aws"]["s3"].get_object(Bucket=env["aws"]["bucket"], Key="processed/documents/d3/extraction/raw/textract_p1.json")
    # rerun is a no-op and does not call Textract again
    assert call(env, "ext", "d3").json()["status"] == "SKIPPED" and env["tx"].calls == 1


def test_ocr_disabled_and_budget_exceeded_fail_permanently(env):
    register(env, "d4", "scan.pdf", make_blank_pdf(), "pdf", "pdf")
    call(env, "norm", "d4"); call(env, "clean", "d4")
    r = call(env, "ext_off", "d4")
    assert r.status_code == 409 and r.json()["error_code"] == "OCR_DISABLED"
    assert env["reg"].get_document("d4")["status"] == "FAILED" and env["tx"].calls == 0
    register(env, "d5", "scan2.pdf", make_blank_pdf(), "pdf", "pdf")
    call(env, "norm", "d5"); call(env, "clean", "d5")
    assert call(env, "ext_cap", "d5").json()["error_code"] == "TEXTRACT_BUDGET_EXCEEDED"


def test_csv_xlsx_and_text_normalization(env):
    import openpyxl

    register(env, "c1", "t.csv", b"name,amount\nAnn,10.50\nBob,20\n", "text", "csv")
    wb = openpyxl.Workbook(); wb.active.title = "Sales"; wb.active.append(["Region", "Total"]); wb.active.append(["North", 99])
    b = io.BytesIO(); wb.save(b)
    register(env, "x1", "s.xlsx", b.getvalue(), "xlsx", "xlsx")
    register(env, "t1", "n.txt", "Café notes about meetings and decisions made.\n\nSecond paragraph here.".encode("cp1252"), "text", "txt")
    for d in ("c1", "x1", "t1"):
        pipeline(env, d)
    assert stored(env, "c1", "extraction", "extraction.json")["data"]["pages"][0]["blocks"][0]["text"] == "name | amount\nAnn | 10.50\nBob | 20"
    xp = stored(env, "x1", "extraction", "extraction.json")["data"]["pages"][0]
    assert xp["sheet"] == "Sales" and xp["blocks"][0]["section"] == "Sales" and "North | 99" in xp["blocks"][0]["text"]
    assert "Café" in stored(env, "t1", "extraction", "extraction.json")["data"]["pages"][0]["blocks"][0]["text"]
    assert env["reg"].get_document("x1")["doc_type"] == "spreadsheet"


def test_citation_locators_carry_archive_origin(env):
    register(env, "z1", "in_zip.docx", make_docx(), "docx", "docx")
    env["reg"].update_document("z1", archive_entry_path="folder/in_zip.docx")
    pipeline(env, "z1")
    src = stored(env, "z1", "metadata", "metadata.json")["data"]["source"]
    assert src["parent_document_id"] == "doc_zip" and src["archive_entry_path"] == "folder/in_zip.docx"


def test_exact_duplicate_is_marked_and_skips_remaining_stages(env):
    data = make_pdf()
    register(env, "a", "one.pdf", data, "pdf", "pdf")
    register(env, "b", "two.pdf", data, "pdf", "pdf")
    assert call(env, "dup", "a").json()["result"]["exact_duplicate"] is False
    r = call(env, "dup", "b").json()["result"]
    assert r == {"exact_duplicate": True, "skip_remaining": True, "duplicate_of": "a", "policy": "SKIP"}
    assert env["reg"].get_document("b")["status"] == "DUPLICATE" and env["reg"].get_document("b")["duplicate_of"] == "a"
    members = {m["document_id"]: m["status"] for m in env["reg"].list_members("ing_1")}
    assert members["b"] == "DUPLICATE" and members["a"] == "PROCESSING"
    # REPROCESS keeps going; invalid policy is rejected; rerun is stable
    register(env, "c", "three.pdf", data, "pdf", "pdf")
    assert call(env, "dup", "c", duplicate_policy="reprocess").json()["result"]["skip_remaining"] is False
    assert call(env, "dup", "c", duplicate_policy="NOPE").status_code == 422
    assert call(env, "dup", "a").json()["result"]["exact_duplicate"] is False


def test_preconditions_and_corruption(env):
    assert call(env, "norm", "missing").status_code == 404
    register(env, "e1", "x.pdf", make_pdf(), "pdf", "pdf")
    assert call(env, "clean", "e1").json()["error_code"] == "PREVIOUS_STAGE_OUTPUT_MISSING"
    register(env, "bad", "bad.docx", b"PK\x03\x04 not really a docx", "docx", "docx")
    r = call(env, "norm", "bad")
    assert r.status_code == 422 and r.json()["error_code"] == "PARSE_FAILED"
    register(env, "cs", "cs.pdf", make_pdf(), "pdf", "pdf")
    env["reg"].update_document("cs", checksum_sha256="f" * 64)
    assert call(env, "norm", "cs").json()["error_code"] == "CHECKSUM_MISMATCH"
    assert env["c"]["norm"].post("/api/v1/process", json={"context": {"document_id": "e1"}}).status_code == 401


def test_cleaning_blocks_changes_to_protected_tokens(env, monkeypatch):
    register(env, "p1", "n.txt", b"Amount 1,250.75 on 2025-03-31 for the team meeting notes.", "text", "txt")
    call(env, "norm", "p1")
    import cleaning_service.logic as lg

    monkeypatch.setattr(lg, "clean_text", lambda t, join_lines=False: t.replace("1,250.75", "1,250.76"))
    r = call(env, "clean", "p1")
    assert r.status_code == 409 and r.json()["error_code"] == "PROTECTED_TOKENS_CHANGED"


def test_audit_rows_exist_for_every_stage(env):
    register(env, "au", "r.docx", make_docx(), "docx", "docx")
    pipeline(env, "au")
    names = {r["service_name"] for r in env["aws"]["trail_table"].scan()["Items"] if r["status"] == "COMPLETED"}
    assert {"normalization-service", "cleaning-service", "duplicate-service", "extraction-service", "metadata-service"} <= names


def make_blank_pdf_pages(n):
    from reportlab.pdfgen import canvas

    b = io.BytesIO()
    c = canvas.Canvas(b)
    for _ in range(n):
        c.rect(50, 50, 100, 100)
        c.showPage()
    c.save()
    return b.getvalue()


def test_ocr_runs_in_bounded_batches_and_resumes(env):
    register(env, "m1", "scan5.pdf", make_blank_pdf_pages(5), "pdf", "pdf")
    call(env, "norm", "m1"); call(env, "clean", "m1")

    def ext():
        return env["c"]["ext"].post("/api/v1/process", headers=AUTH, json={"context": {"document_id": "m1"}, "parameters": {"ocr_pages_per_call": 2}}).json()

    r1 = ext()
    assert r1["result"] == {"complete": False, "ocr_pages_done": 2, "ocr_pages_remaining": 3} and env["tx"].calls == 2
    assert env["reg"].get_document("m1")["stage"] != "EXTRACTED"
    r2 = ext()
    assert r2["result"]["ocr_pages_remaining"] == 1 and env["tx"].calls == 4
    r3 = ext()
    assert r3["status"] == "COMPLETED" and r3["result"]["complete"] is True and r3["result"]["ocr_pages"] == 5 and env["tx"].calls == 5
    assert env["reg"].get_document("m1")["stage"] == "EXTRACTED"
    assert ext()["status"] == "SKIPPED" and env["tx"].calls == 5  # finished work is never repeated
    pages = stored(env, "m1", "extraction", "extraction.json")["data"]["pages"]
    assert [p["method"] for p in pages] == ["textract"] * 5
