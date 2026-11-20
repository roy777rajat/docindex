import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.registry import Registry
from cleandocs_shared.utils import raw_original_key, s3_uri, sha256_bytes
from validation_service.logic import ValidationDeps
from validation_service.main import SERVICE_NAME, VERSION, create_app

AUTH = {"Authorization": "Bearer s3cret"}


# ---------- sample file builders ----------
def make_pdf(pages=1, password=None) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=100, height=100)
    if password:
        w.encrypt(password)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def make_png() -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(b, "PNG")
    return b.getvalue()


def make_zip(files: dict[str, bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


DOCX = make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>"})
XLSX = make_zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>"})


@pytest.fixture
def env(aws_env):
    settings = Settings(
        service_name=SERVICE_NAME, service_version=VERSION, s3_bucket=aws_env["bucket"],
        service_auth_secret=SecretStr("s3cret"), max_file_size_mb=1, max_pdf_pages=3,
        max_archive_file_count=5, max_archive_expanded_size_mb=1,
    )
    reg = Registry(aws_env["registry_table"])
    deps = ValidationDeps(settings=settings, s3=aws_env["s3"], registry=reg,
                          audit=AuditWriter(aws_env["trail_table"], SERVICE_NAME, VERSION))
    client = TestClient(create_app(settings, deps), raise_server_exceptions=False)
    return client, aws_env, reg


def register(env, data: bytes, filename: str, declared: str | None = None, claimed_checksum: str | None = None):
    _, aws, reg = env
    doc_id, ing = f"doc_{abs(hash((filename, data)))}", "ing_1"
    key = raw_original_key(doc_id, filename)
    aws["s3"].put_object(Bucket=aws["bucket"], Key=key, Body=data)
    reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(
        document_id=doc_id, ingestion_id=ing, original_filename=filename, extension=filename.rsplit(".", 1)[-1].lower(),
        size_bytes=len(data), checksum_sha256=claimed_checksum or sha256_bytes(data),
        s3_uri_original=s3_uri(aws["bucket"], key), content_type_declared=declared,
    )
    reg.add_member(ingestion_id=ing, document_id=doc_id)
    return doc_id


def validate(env, doc_id):
    return env[0].post("/api/v1/process", headers=AUTH, json={"context": {"document_id": doc_id, "trace_id": "t"}})


# ---------- accepted files ----------
@pytest.mark.parametrize(
    "name,data,kind,declared",
    [
        ("a.pdf", make_pdf(2), "pdf", "application/pdf"),
        ("b.png", make_png(), "png", "image/png"),
        ("c.docx", DOCX, "docx", None),
        ("d.xlsx", XLSX, "xlsx", "application/octet-stream"),
        ("e.csv", b"a,b\n1,2\n", "csv", "application/vnd.ms-excel"),
        ("f.txt", b"hello world\n", "txt", "text/plain"),
        ("g.json", b'{"a": [1, 2]}', "json", "application/json"),
        ("h.xml", b"<root><a>1</a></root>", "xml", "text/xml"),
    ],
)
def test_valid_files_are_accepted_and_recorded(env, name, data, kind, declared):
    doc_id = register(env, data, name, declared)
    r = validate(env, doc_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "COMPLETED" and body["result"]["file_kind"] == kind and body["result"]["is_archive"] is False
    doc = env[2].get_document(doc_id)
    assert doc["stage"] == "VALIDATED" and doc["file_kind"] == kind and doc["status"] == "PROCESSING"
    stored = json.loads(env[1]["s3"].get_object(Bucket=env[1]["bucket"], Key=f"processed/documents/{doc_id}/metadata/validation.json")["Body"].read())
    assert stored["valid"] is True and stored["input_checksum"] == sha256_bytes(data)
    assert "hello" not in json.dumps(stored)  # no document content in artifacts


def test_zip_is_flagged_as_archive_and_member_becomes_archive_role(env):
    doc_id = register(env, make_zip({"a.txt": b"x"}), "bundle.zip", "application/zip")
    body = validate(env, doc_id).json()
    assert body["result"]["is_archive"] is True and body["result"]["file_kind"] == "zip"
    assert env[2].list_members("ing_1")[0]["role"] == "ARCHIVE"


# ---------- rejections ----------
@pytest.mark.parametrize(
    "name,data,declared,status,code",
    [
        ("evil.pdf", b"MZ\x90\x00" + b"\x00" * 100, None, 422, "SIGNATURE_MISMATCH"),          # exe renamed to pdf
        ("photo.png", make_pdf(), None, 422, "SIGNATURE_MISMATCH"),                              # pdf renamed to png
        ("doc.docx", make_zip({"a.txt": b"x"}), None, 422, "SIGNATURE_MISMATCH"),                # plain zip as docx
        ("deck.pptx", b"PK\x03\x04", None, 415, "EXTENSION_NOT_ALLOWED"),
        ("tool.exe", b"MZ", None, 415, "EXTENSION_NOT_ALLOWED"),
        ("noext", b"hello", None, 415, "EXTENSION_NOT_ALLOWED"),
        ("a.pdf", make_pdf(), "image/png", 422, "MIME_MISMATCH"),
        ("big.pdf", make_pdf() + b"0" * (1024 * 1024 + 1), None, 422, "FILE_TOO_LARGE"),
        ("many.pdf", make_pdf(4), None, 422, "PAGE_LIMIT_EXCEEDED"),
        ("locked.pdf", make_pdf(1, "pw"), None, 422, "ENCRYPTED_DOCUMENT"),
        ("broken.pdf", b"%PDF-1.4\n1 0 obj\n<<garbage", None, 422, "CORRUPT_FILE"),
        ("bad.json", b"{not json", None, 422, "INVALID_JSON"),
        ("bad.xml", b"<a><b></a>", None, 422, "INVALID_XML"),
        ("bin.csv", b"a,b\x00\x00\x01\x02" * 50, None, 422, "SIGNATURE_MISMATCH"),
        ("trunc.png", make_png()[:30], None, 422, "CORRUPT_FILE"),
        ("enc.zip", None, None, 422, "ENCRYPTED_ARCHIVE"),
        ("wide.zip", make_zip({f"{i}.txt": b"x" for i in range(6)}), None, 422, "ARCHIVE_LIMIT_EXCEEDED"),
    ],
)
def test_rejections_are_specific_and_recorded(env, name, data, declared, status, code):
    if name == "enc.zip":  # zipfile clears the flag on write, so set the encryption bit in the raw headers
        raw = bytearray(make_zip({"secret.txt": b"x"}))
        raw[raw.index(b"PK\x03\x04") + 6] |= 0x1
        raw[raw.index(b"PK\x01\x02") + 8] |= 0x1
        data = bytes(raw)
    doc_id = register(env, data, name, declared)
    r = validate(env, doc_id)
    assert r.status_code == status, r.text
    assert r.json()["error_code"] == code and r.json()["retryable"] is False
    doc = env[2].get_document(doc_id)
    assert doc["status"] == "REJECTED" and doc["rejection_code"] == code
    assert env[2].list_members("ing_1")[0]["status"] == "REJECTED"
    failed = [x for x in env[1]["trail_table"].scan()["Items"] if x["status"] == "FAILED"]
    assert failed and failed[0]["error_code"] == code and failed[0]["error_class"] in ("VALIDATION", "UNSUPPORTED_FORMAT")


def test_xml_with_entities_is_rejected_as_unsafe(env):
    bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><l>&b;</l>'
    doc_id = register(env, bomb, "bomb.xml")
    r = validate(env, doc_id)
    assert r.status_code == 422 and r.json()["error_code"] == "UNSAFE_XML"


def test_checksum_mismatch_is_detected(env):
    doc_id = register(env, make_pdf(), "a.pdf", claimed_checksum="f" * 64)
    r = validate(env, doc_id)
    assert r.status_code == 422 and r.json()["error_code"] == "CHECKSUM_MISMATCH"


def test_unknown_document_and_foreign_bucket(env):
    assert validate(env, "doc_missing").status_code == 404
    doc_id = register(env, make_pdf(), "a.pdf")
    r = env[0].post("/api/v1/process", headers=AUTH, json={
        "context": {"document_id": doc_id}, "input_s3_uri": "s3://someone-elses-bucket/x.pdf"})
    assert r.status_code == 422 and r.json()["error_code"] == "FOREIGN_BUCKET"


def test_second_run_is_skipped_idempotently(env):
    doc_id = register(env, make_pdf(), "a.pdf")
    first = validate(env, doc_id).json()
    second = validate(env, doc_id).json()
    assert first["status"] == "COMPLETED" and second["status"] == "SKIPPED"
    assert second["result"] == first["result"] and "already completed" in second["skipped_reason"]
    statuses = sorted(x["status"] for x in env[1]["trail_table"].scan()["Items"])
    assert statuses.count("SKIPPED") == 1 and statuses.count("COMPLETED") == 1 and statuses.count("STARTED") == 2


def test_requires_auth_and_health(env):
    c = env[0]
    assert c.post("/api/v1/process", json={"context": {"document_id": "d"}}).status_code == 401
    assert c.get("/health/ready").status_code == 200
