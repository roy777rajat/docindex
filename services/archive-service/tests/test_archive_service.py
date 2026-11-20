import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from archive_service import safety
from archive_service.logic import ArchiveDeps
from archive_service.main import SERVICE_NAME, VERSION, create_app
from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.registry import Registry
from cleandocs_shared.utils import raw_original_key, s3_uri, sha256_bytes

AUTH = {"Authorization": "Bearer s3cret"}
PDF = b"%PDF-1.4\n%fake but signature-valid\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
EXE = b"MZ\x90\x00" + b"\x00" * 64


def make_zip(entries, infos=None) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
        for zi, data in (infos or []):
            z.writestr(zi, data)
    return b.getvalue()


def build(aws_env, **overrides):
    cfg = dict(service_name=SERVICE_NAME, service_version=VERSION, s3_bucket=aws_env["bucket"],
               service_auth_secret=SecretStr("s3cret"), max_file_size_mb=1, max_archive_file_count=5,
               max_archive_expanded_size_mb=3, max_archive_depth=0)
    cfg.update(overrides)
    settings = Settings(**cfg)
    reg = Registry(aws_env["registry_table"])
    deps = ArchiveDeps(settings=settings, s3=aws_env["s3"], registry=reg, audit=AuditWriter(aws_env["trail_table"], SERVICE_NAME, VERSION))
    return TestClient(create_app(settings, deps), raise_server_exceptions=False), reg


def register(aws_env, reg, zip_bytes, parent="doc_parent", ing="ing_1", depth=0, is_archive=True, checksum=None):
    key = raw_original_key(parent, "bundle.zip")
    aws_env["s3"].put_object(Bucket=aws_env["bucket"], Key=key, Body=zip_bytes)
    reg.create_ingestion(ingestion_id=ing, trace_id="t")
    reg.create_document(document_id=parent, ingestion_id=ing, original_filename="bundle.zip", extension="zip",
                        size_bytes=len(zip_bytes), checksum_sha256=checksum or sha256_bytes(zip_bytes),
                        s3_uri_original=s3_uri(aws_env["bucket"], key), archive_depth=depth)
    reg.update_document(parent, stage="VALIDATED", is_archive=is_archive, file_kind="zip")
    reg.add_member(ingestion_id=ing, document_id=parent, role="ARCHIVE")
    return parent


def extract(client, parent="doc_parent"):
    return client.post("/api/v1/process", headers=AUTH, json={"context": {"document_id": parent, "trace_id": "t"}})


# ---------------- happy path ----------------
def test_extracts_supported_files_into_independent_children(aws_env):
    client, reg = build(aws_env, max_archive_file_count=10)
    z = make_zip({"reports/Medical Report.pdf": PDF, "scan.png": PNG, "notes/a.txt": b"hello", "__MACOSX/._x": b"junk",
                  ".DS_Store": b"x", "tool.exe": EXE, "folder/": b""})
    register(aws_env, reg, z)
    r = extract(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "COMPLETED" and body["result"]["accepted"] == 3
    assert body["result"]["rejected_entries"] == [{"entry_path": "tool.exe", "reason": "EXTENSION_NOT_ALLOWED"}]
    assert body["result"]["ignored"] == 2

    children = body["result"]["children"]
    assert len({c["document_id"] for c in children}) == 3  # independent document ids
    for c in children:
        doc = reg.get_document(c["document_id"])
        assert doc["parent_document_id"] == "doc_parent" and doc["archive_entry_path"] == c["entry_path"]
        assert doc["archive_depth"] == 1 and doc["status"] == "PROCESSING"
        key = c["s3_uri"].split(f"s3://{aws_env['bucket']}/")[1]
        assert key.startswith(f"raw/documents/{c['document_id']}/original/")
        data = aws_env["s3"].get_object(Bucket=aws_env["bucket"], Key=key)["Body"].read()
        assert sha256_bytes(data) == c["checksum_sha256"] == doc["checksum_sha256"]  # byte-exact

    parent = reg.get_document("doc_parent")
    assert parent["stage"] == "EXTRACTED_ARCHIVE" and parent["child_count"] == 3 and parent["status"] == "PROCESSING"
    members = {m["document_id"]: m for m in reg.list_members("ing_1")}
    assert members["doc_parent"]["role"] == "ARCHIVE"
    statuses = [m["status"] for m in members.values() if m["document_id"] != "doc_parent"]
    assert statuses.count("PROCESSING") == 3 and statuses.count("REJECTED") == 1  # rejected entry is accounted for

    manifest = json.loads(aws_env["s3"].get_object(Bucket=aws_env["bucket"], Key="processed/documents/doc_parent/manifests/archive_manifest.json")["Body"].read())
    assert {e["status"] for e in manifest["entries"]} == {"ACCEPTED", "REJECTED", "IGNORED"}
    assert "hello" not in json.dumps(manifest)  # no content in the manifest


def test_rerun_is_idempotent_and_skips(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"a.pdf": PDF, "b.txt": b"x"}))
    first = extract(client).json()
    second = extract(client).json()
    assert second["status"] == "SKIPPED" and second["result"]["children"] == first["result"]["children"]
    assert len(reg.list_members("ing_1")) == 3


def test_retry_after_partial_run_reuses_same_child_ids(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"a.pdf": PDF, "dir/b.txt": b"x"}))
    first = extract(client).json()
    # simulate a crash after children were written but before the manifest/stage update
    reg.update_document("doc_parent", stage="VALIDATED")
    aws_env["s3"].delete_object(Bucket=aws_env["bucket"], Key="processed/documents/doc_parent/manifests/archive_manifest.json")
    again = extract(client)
    assert again.status_code == 200 and again.json()["status"] == "COMPLETED"
    assert [c["document_id"] for c in again.json()["result"]["children"]] == [c["document_id"] for c in first["result"]["children"]]
    assert len(reg.list_members("ing_1")) == 3  # no duplicate children


# ---------------- ZIP security ----------------
@pytest.mark.parametrize("evil", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/Windows/x.txt", "..\\evil.txt"])
def test_zip_slip_and_absolute_paths_reject_the_whole_archive(aws_env, evil):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"good.pdf": PDF, evil: b"x"}))
    r = extract(client)
    assert r.status_code == 422 and r.json()["error_code"] == "UNSAFE_ENTRY_PATH"
    assert reg.get_document("doc_parent")["status"] == "REJECTED"
    assert aws_env["s3"].list_objects_v2(Bucket=aws_env["bucket"], Prefix="raw/documents/").get("KeyCount") == 1  # only the parent; no child written


def test_symlink_entry_rejects_archive(aws_env):
    client, reg = build(aws_env)
    zi = zipfile.ZipInfo("link.txt")
    zi.external_attr = (0o120777 << 16)
    register(aws_env, reg, make_zip({"a.pdf": PDF}, infos=[(zi, b"/etc/passwd")]))
    r = extract(client)
    assert r.status_code == 422 and r.json()["error_code"] == "UNSAFE_ENTRY_TYPE"


def test_too_many_files_and_oversized_expansion(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({f"f{i}.txt": b"x" for i in range(6)}))
    assert extract(client).json()["error_code"] == "ARCHIVE_LIMIT_EXCEEDED"

    client2, reg2 = build(aws_env, max_archive_expanded_size_mb=1)
    register(aws_env, reg2, make_zip({"a.txt": b"a" * 600_000, "b.txt": b"b" * 600_000}), parent="doc_big", ing="ing_2")
    assert extract(client2, "doc_big").json()["error_code"] == "ARCHIVE_LIMIT_EXCEEDED"


def test_decompression_bomb_ratio_is_detected(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"zeros.txt": b"\x00" * 900_000}))  # under the size limits but ratio ~1000:1
    r = extract(client)
    assert r.status_code == 422 and r.json()["error_code"] == "BOMB_SUSPECTED"


def test_encrypted_entry_rejects_archive(aws_env):
    client, reg = build(aws_env)
    raw = bytearray(make_zip({"s.txt": b"x"}))
    raw[raw.index(b"PK\x03\x04") + 6] |= 1
    raw[raw.index(b"PK\x01\x02") + 8] |= 1
    register(aws_env, reg, bytes(raw))
    assert extract(client).json()["error_code"] == "ENCRYPTED_ARCHIVE"


def test_nested_archives_follow_depth_policy(aws_env):
    inner = make_zip({"x.txt": b"hi"})
    client, reg = build(aws_env)  # depth 0: nested not allowed
    register(aws_env, reg, make_zip({"a.pdf": PDF, "inner.zip": inner}))
    body = extract(client).json()
    assert body["result"]["rejected_entries"] == [{"entry_path": "inner.zip", "reason": "NESTED_ARCHIVE_NOT_ALLOWED"}]

    client2, reg2 = build(aws_env, max_archive_depth=1)  # depth 1: allowed once
    register(aws_env, reg2, make_zip({"inner.zip": inner}), parent="doc_p2", ing="ing_2")
    ok = extract(client2, "doc_p2").json()
    assert ok["result"]["accepted"] == 1 and reg2.get_document(ok["result"]["children"][0]["document_id"])["archive_depth"] == 1
    # a child archive already at depth 1 cannot nest further
    register(aws_env, reg2, make_zip({"inner2.zip": inner}), parent="doc_p3", ing="ing_3", depth=1)
    assert extract(client2, "doc_p3").status_code == 422  # all entries rejected -> NO_SUPPORTED_FILES


def test_entry_level_rejections_do_not_fail_siblings(aws_env):
    client, reg = build(aws_env)
    z = make_zip({"ok.pdf": PDF, "fake.pdf": EXE, "big.txt": b"".join(bytes([i % 251]) for i in range(1_100_000)),
                  "empty.txt": b""})
    register(aws_env, reg, z)
    body = extract(client).json()
    reasons = {e["entry_path"]: e["reason"] for e in body["result"]["rejected_entries"]}
    assert reasons == {"fake.pdf": "SIGNATURE_MISMATCH", "big.txt": "FILE_TOO_LARGE", "empty.txt": "EMPTY_FILE"}
    assert body["result"]["accepted"] == 1


def test_archive_with_only_unsupported_files_is_rejected_but_evidence_is_kept(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"a.exe": EXE, "b.dll": EXE}))
    r = extract(client)
    assert r.status_code == 422 and r.json()["error_code"] == "NO_SUPPORTED_FILES"
    assert reg.get_document("doc_parent")["status"] == "REJECTED"
    assert aws_env["s3"].get_object(Bucket=aws_env["bucket"], Key="processed/documents/doc_parent/manifests/archive_manifest.json")


def test_duplicate_entry_names_inside_one_archive(aws_env):
    client, reg = build(aws_env)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("a.txt", b"one")
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            z.writestr("a.txt", b"two")
    register(aws_env, reg, b.getvalue())
    body = extract(client).json()
    assert body["result"]["accepted"] == 1 and body["result"]["rejected_entries"][0]["reason"] == "DUPLICATE_ENTRY_PATH"


# ---------------- preconditions, audit, safety helpers ----------------
def test_preconditions(aws_env):
    client, reg = build(aws_env)
    assert extract(client, "doc_missing").status_code == 404
    register(aws_env, reg, make_zip({"a.txt": b"x"}), is_archive=False)
    assert extract(client).json()["error_code"] == "NOT_AN_ARCHIVE"
    client2, reg2 = build(aws_env)
    register(aws_env, reg2, make_zip({"a.txt": b"x"}), parent="doc_cs", ing="ing_x", checksum="f" * 64)
    assert extract(client2, "doc_cs").json()["error_code"] == "CHECKSUM_MISMATCH"
    assert client.post("/api/v1/process", json={"context": {"document_id": "d"}}).status_code == 401


def test_audit_records_children_and_rejections(aws_env):
    client, reg = build(aws_env)
    register(aws_env, reg, make_zip({"a.pdf": PDF, "x.exe": EXE}))
    extract(client)
    rows = [r for r in aws_env["trail_table"].scan()["Items"] if r["service_name"] == "archive-service"]
    done = [r for r in rows if r["status"] == "COMPLETED"][0]
    assert done["metrics"]["children_accepted"] == 1 and done["metrics"]["entries_rejected"] == 1
    assert done["output_s3_uri"].endswith("archive_manifest.json")


@pytest.mark.parametrize("name,ok", [("a/b.txt", True), ("./a.txt", True), ("a//b.txt", True), ("a/../b.txt", False), ("/x", False), ("c:x.txt", False)])
def test_normalize_entry_path(name, ok):
    if ok:
        assert ".." not in safety.normalize_entry_path(name) and not safety.normalize_entry_path(name).startswith("/")
    else:
        with pytest.raises(safety.ArchiveFatal):
            safety.normalize_entry_path(name)


def test_child_ids_are_deterministic_and_distinct():
    assert safety.child_document_id("p", "a.txt") == safety.child_document_id("p", "a.txt")
    assert safety.child_document_id("p", "a.txt") != safety.child_document_id("p", "b.txt")
    assert safety.child_document_id("p", "a.txt") != safety.child_document_id("q", "a.txt")
