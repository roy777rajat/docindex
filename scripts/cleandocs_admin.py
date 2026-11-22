"""CleanDocs admin tool: list, delete one document/ingestion, or delete everything. Uses your default AWS credentials.

  list                         every document: id, name, owner, status, ingestion
  delete <id> [<id> ...]       delete by document id (doc_...) or ingestion id (ing_...); a ZIP deletes its children too
  delete-all                   wipe ALL document data (calls clean_all.py logic; asks you to type DELETE ALL)
Add --dry-run to `delete` to see what would go. Infrastructure is never touched.

What `delete` removes for each document: S3 raw/, processed/, kb-source/ (every version), registry rows (document, ingestion
membership, duplicate-checksum claim, idempotency keys of that upload), audit-trail rows, then syncs the Knowledge Base so the
vectors disappear. Deleting the file's claim means you can upload the same file again without it being flagged DUPLICATE.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import boto3
from boto3.dynamodb.conditions import Key

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cfg  # noqa: E402
import clean_all  # noqa: E402


def query_all(table, pk):
    out, kw = [], {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        r = table.query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def scan_all(table, **kw):
    out = []
    while True:
        r = table.scan(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def all_documents(reg):
    items = scan_all(reg, FilterExpression="begins_with(PK, :p) AND SK = :m", ExpressionAttributeValues={":p": "DOC#", ":m": "META"})
    return {i["document_id"]: i for i in items}


def expand(reg, ids):
    """Resolve ing_ ids to their documents and add children of ZIP parents. Returns (doc_ids, ingestion_ids)."""
    docs = all_documents(reg)
    chosen, ings = set(), set()
    for i in ids:
        if i.startswith("ing_"):
            ings.add(i)
            chosen |= {d for d, v in docs.items() if v.get("ingestion_id") == i}
        elif i in docs:
            chosen.add(i)
        else:
            print(f"  not found in registry: {i} (S3 leftovers for it are still removed)")
            chosen.add(i)
    chosen |= {d for d, v in docs.items() if v.get("parent_document_id") in chosen}
    ings |= {docs[d]["ingestion_id"] for d in chosen if d in docs}
    return chosen, ings, docs


def delete_prefix(s3, bucket, prefix, dry):
    batch, n = [], 0
    for item in clean_all.s3_versions(s3, bucket, prefix):
        n += 1
        batch.append(item)
        if len(batch) == 1000 and not dry:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            batch = []
    if batch and not dry:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
    return n


def delete_documents(s3, ddb, cfg, ids, dry):
    reg, trail = ddb.Table(cfg["registry"]), ddb.Table(cfg["trail"])
    chosen, ings, docs = expand(reg, ids)
    if not chosen:
        print("nothing to delete")
        return 0
    print(f"{'WOULD DELETE' if dry else 'DELETING'} {len(chosen)} document(s) from {len(ings)} ingestion(s)")
    idemp = scan_all(reg, FilterExpression="begins_with(PK, :p)", ExpressionAttributeValues={":p": "IDEMP#"})
    for d in sorted(chosen):
        info = docs.get(d, {})
        s3n = sum(delete_prefix(s3, cfg["bucket"], f"{p}/{d}/" if p != "raw" else f"raw/documents/{d}/", dry)
                  for p in ("raw", "kb-source")) + delete_prefix(s3, cfg["bucket"], f"processed/documents/{d}/", dry)
        rows = query_all(reg, f"DOC#{d}")
        for r in rows:
            if not dry:
                reg.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
        if info.get("checksum_sha256"):
            claim = reg.get_item(Key={"PK": f"CHECKSUM#{info['checksum_sha256']}", "SK": "CLAIM"}).get("Item")
            if claim and claim.get("canonical_document_id") == d and not dry:
                reg.delete_item(Key={"PK": claim["PK"], "SK": "CLAIM"})
        if info.get("ingestion_id") and not dry:
            reg.delete_item(Key={"PK": f"INGEST#{info['ingestion_id']}", "SK": f"DOC#{d}"})
        trows = query_all(trail, f"DOCUMENT#{d}")
        if not dry:
            with trail.batch_writer() as bw:
                for r in trows:
                    bw.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
        print(f"  {d}  {info.get('original_filename', '?'):<32} S3 {s3n} versions, registry {len(rows)} rows, trail {len(trows)} rows")
    for ing in sorted(ings):
        left = [m for m in query_all(reg, f"INGEST#{ing}") if m["SK"].startswith("DOC#") and m["SK"][4:] not in chosen]
        if not left:  # ingestion fully removed
            for r in query_all(reg, f"INGEST#{ing}") + query_all(trail, f"INGESTION#{ing}"):
                if not dry:
                    (trail if r["PK"].startswith("INGESTION#") else reg).delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
            for r in idemp:
                if r.get("response", {}).get("ingestion_id") == ing and not dry:
                    reg.delete_item(Key={"PK": r["PK"], "SK": r["SK"]})
            print(f"  ingestion {ing} removed (rows, idempotency key)")
    return len(chosen)


def cmd_list(ddb, cfg):
    docs = all_documents(ddb.Table(cfg["registry"]))
    if not docs:
        print("no documents")
    for d, v in sorted(docs.items(), key=lambda kv: kv[1].get("uploaded_at", "")):
        parent = f" (in {v['parent_document_id']})" if v.get("parent_document_id") else ""
        print(f"{d}  {v.get('status', '?'):<10} {v.get('stage', '?'):<18} owner={v.get('owner', 'ADMIN'):<10} "
              f"{v.get('original_filename', '')}{parent}  {v.get('ingestion_id', '')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="eu-west-1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    d = sub.add_parser("delete")
    d.add_argument("ids", nargs="+")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--no-sync", action="store_true", help="skip the Knowledge Base sync (vectors stay until the next sync)")
    sub.add_parser("delete-all")
    a = ap.parse_args()

    if a.cmd == "delete-all":
        if input("This permanently deletes ALL documents, registry rows, trail and vectors.\nType DELETE ALL to continue: ").strip() != "DELETE ALL":
            print("cancelled")
            return 1
        acct = boto3.Session(region_name=a.region).client("sts").get_caller_identity()["Account"]
        return subprocess.call([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "clean_all.py"),
                                "--region", a.region, "--yes", f"DELETE-ALL-{acct}"])
    cfg = _cfg.load(a.region)
    sess = boto3.Session(region_name=a.region)
    ddb, s3 = sess.resource("dynamodb"), sess.client("s3")
    if a.cmd == "list":
        cmd_list(ddb, cfg)
        return 0
    if clean_all.running_executions(sess.client("stepfunctions")):
        print("Step Functions executions are RUNNING; wait or stop them first.")
        return 2
    n = delete_documents(s3, ddb, cfg, a.ids, a.dry_run)
    if a.dry_run or not n or a.no_sync:
        return 0
    status, stats = clean_all.sync_kb(sess.client("bedrock-agent"), cfg["kb_id"], cfg["ds_id"])
    print(f"Knowledge Base sync {status}: documents deleted {stats.get('numberOfDocumentsDeleted')}, failed {stats.get('numberOfDocumentsFailed')}")
    return 0 if status == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
