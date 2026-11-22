"""scripts/cleandocs_admin.py: delete one document / a ZIP with its children / an ingestion, against mocked AWS."""
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import cleandocs_admin as adm  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402


def seed(aws_env):
    reg = Registry(aws_env["registry_table"])
    reg.create_ingestion(ingestion_id="ing_1", trace_id="t", owner="bob")
    for doc, parent, sha in (("doc_zip", None, "z" * 64), ("doc_a", "doc_zip", "a" * 64), ("doc_b", "doc_zip", "b" * 64), ("doc_other", None, "o" * 64)):
        ing = "ing_2" if doc == "doc_other" else "ing_1"
        if doc == "doc_other":
            reg.create_ingestion(ingestion_id="ing_2", trace_id="t")
        reg.create_document(document_id=doc, ingestion_id=ing, original_filename=doc + ".pdf", extension="pdf", size_bytes=1,
                            checksum_sha256=sha, s3_uri_original="s3://b/x", parent_document_id=parent)
        reg.add_member(ingestion_id=ing, document_id=doc)
        reg.claim_checksum(sha, doc)
        for key in (f"raw/documents/{doc}/original/{doc}.pdf", f"processed/documents/{doc}/x.json", f"kb-source/{doc}/c.txt"):
            aws_env["s3"].put_object(Bucket=aws_env["bucket"], Key=key, Body=b"x")
        aws_env["trail_table"].put_item(Item={"PK": f"DOCUMENT#{doc}", "SK": "s#1"})
    reg.store_idempotent_response("key-for-ing1", {"ingestion_id": "ing_1"})
    reg.store_idempotent_response("key-for-ing2", {"ingestion_id": "ing_2"})
    return reg


def keys(aws_env):
    r = aws_env["s3"].list_objects_v2(Bucket=aws_env["bucket"]).get("Contents", [])
    return {o["Key"] for o in r}


def run(aws_env, ids, dry=False):
    ddb = boto3.resource("dynamodb", region_name="eu-west-1")
    cfg = {"bucket": aws_env["bucket"], "registry": aws_env["registry_table"].name, "trail": aws_env["trail_table"].name}
    return adm.delete_documents(aws_env["s3"], ddb, cfg, ids, dry)


def test_dry_run_deletes_nothing(aws_env):
    reg = seed(aws_env)
    before = keys(aws_env)
    assert run(aws_env, ["doc_zip"], dry=True) == 3
    assert keys(aws_env) == before and reg.get_document("doc_a") is not None


def test_deleting_a_zip_removes_children_claims_and_idempotency_but_not_other_uploads(aws_env):
    reg = seed(aws_env)
    assert run(aws_env, ["doc_zip"]) == 3
    left = keys(aws_env)
    assert all("doc_other" in k for k in left) and len(left) == 3
    for d in ("doc_zip", "doc_a", "doc_b"):
        assert reg.get_document(d) is None
    assert reg.get_document("doc_other") is not None
    assert reg.get_idempotent_response("key-for-ing1") is None and reg.get_idempotent_response("key-for-ing2") is not None
    assert reg.get_ingestion("ing_1") is None and reg.get_ingestion("ing_2") is not None
    assert reg.claim_checksum("a" * 64, "doc_new") == "doc_new"      # re-upload is no longer a duplicate
    assert reg.claim_checksum("o" * 64, "doc_new") == "doc_other"    # untouched claim stays
    assert aws_env["trail_table"].get_item(Key={"PK": "DOCUMENT#doc_a", "SK": "s#1"}).get("Item") is None


def test_delete_by_ingestion_id(aws_env):
    reg = seed(aws_env)
    assert run(aws_env, ["ing_2"]) == 1
    assert reg.get_document("doc_other") is None and reg.get_document("doc_a") is not None
