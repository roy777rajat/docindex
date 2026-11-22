"""Full clean of CleanDocs DATA (never infrastructure): S3 project prefixes, DynamoDB items, Knowledge Base vectors.

  python scripts\\clean_all.py                         dry run: counts only, deletes nothing
  python scripts\\clean_all.py --yes DELETE-ALL-<acct>  really delete (the confirm phrase contains your AWS account id)

What it removes
  S3        every version under raw/ processed/ kb-source/ tmp/ (nothing else in the bucket)
  Registry  DOC#, INGEST#, CHECKSUM#, IDEMP# items (so a re-uploaded file is NOT flagged DUPLICATE); GATE# lock;
            BUDGET# counters only with --reset-budgets
  Trail     every row of document-ingest-trail
  KB        starts a data-source sync (S3 is now empty, so Bedrock removes the vectors) and waits for it, then counts vectors
Refuses while a Step Functions execution is RUNNING. Keeps: bucket, tables, queues, state machines, KB, vector index, IAM, Render.
Uses your default AWS credentials (admin). Overrides: --region --kb-id --ds-id --vector-bucket --index
"""
from __future__ import annotations

import argparse
import sys
import time

import boto3

sys.path.insert(0, __import__("os").path.dirname(__file__))
import _cfg  # noqa: E402

PREFIXES = ["raw/", "processed/", "kb-source/", "tmp/"]
REGISTRY_PREFIXES = ["DOC#", "INGEST#", "CHECKSUM#", "IDEMP#", "GATE#"]


def s3_versions(s3, bucket, prefix):
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
            yield {"Key": v["Key"], "VersionId": v["VersionId"]}


def clean_s3(s3, bucket, dry):
    total = 0
    for prefix in PREFIXES:
        batch, n = [], 0
        for item in s3_versions(s3, bucket, prefix):
            n += 1
            if not dry:
                batch.append(item)
                if len(batch) == 1000:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
                    batch = []
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        print(f"  S3 {prefix:<12} {n} object versions")
        total += n
    return total


def scan_keys(table):
    kw = {"ProjectionExpression": "PK, SK"}
    while True:
        r = table.scan(**kw)
        yield from r["Items"]
        if "LastEvaluatedKey" not in r:
            return
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def clean_table(table, keep, dry, label):
    n = 0
    items = [i for i in scan_keys(table) if keep(i["PK"])]
    n = len(items)
    if not dry:
        with table.batch_writer() as bw:
            for i in items:
                bw.delete_item(Key={"PK": i["PK"], "SK": i["SK"]})
    print(f"  DynamoDB {label:<22} {n} items")
    return n


def running_executions(sfn):
    out = []
    for sm in sfn.list_state_machines()["stateMachines"]:
        if sm["name"].startswith("cleandocs-"):
            out += [e["executionArn"] for e in sfn.list_executions(stateMachineArn=sm["stateMachineArn"], statusFilter="RUNNING")["executions"]]
    return out


def vector_count(v, bucket, index):
    n, token = 0, None
    while True:
        kw = {"vectorBucketName": bucket, "indexName": index, "maxResults": 500}
        if token:
            kw["nextToken"] = token
        r = v.list_vectors(**kw)
        n += len(r.get("vectors", []))
        token = r.get("nextToken")
        if not token:
            return n


def sync_kb(agent, kb, ds, timeout=900):
    job = agent.start_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds)["ingestionJob"]["ingestionJobId"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = agent.get_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds, ingestionJobId=job)["ingestionJob"]
        if j["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            return j["status"], j.get("statistics", {})
        time.sleep(10)
    return "TIMEOUT", {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", default="", help="confirm phrase DELETE-ALL-<account-id>")
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--kb-id", default=None)
    ap.add_argument("--ds-id", default=None)
    ap.add_argument("--vector-bucket", default=None)
    ap.add_argument("--index", default=None)
    ap.add_argument("--reset-budgets", action="store_true")
    a = ap.parse_args()
    sess = boto3.Session(region_name=a.region)
    cfg = _cfg.load(a.region)
    acct, bucket = cfg["account"], cfg["bucket"]
    vbucket, a.kb_id, a.ds_id = a.vector_bucket or cfg["vector_bucket"], a.kb_id or cfg["kb_id"], a.ds_id or cfg["ds_id"]
    a.index = a.index or cfg["index"]
    dry = a.yes != f"DELETE-ALL-{acct}"
    if a.yes and dry:
        raise SystemExit(f"wrong confirm phrase; expected DELETE-ALL-{acct}")
    print(("DRY RUN (nothing is deleted)" if dry else "DELETING") + f"  account {acct}  bucket {bucket}")

    running = running_executions(sess.client("stepfunctions"))
    if running:
        print("Step Functions executions are RUNNING; stop them first (or wait):\n  " + "\n  ".join(running))
        return 2

    s3, ddb, v = sess.client("s3"), sess.resource("dynamodb"), sess.client("s3vectors")
    clean_s3(s3, bucket, dry)
    keep_budget = a.reset_budgets
    clean_table(ddb.Table(cfg["registry"]), lambda pk: pk.split("#")[0] + "#" in REGISTRY_PREFIXES or (keep_budget and pk.startswith("BUDGET#")), dry, "registry")
    clean_table(ddb.Table(cfg["trail"]), lambda pk: True, dry, "document-ingest-trail")
    print(f"  vectors now in index        {vector_count(v, vbucket, a.index)}")
    if dry:
        print(f"\nTo delete for real:  python scripts\\clean_all.py --yes DELETE-ALL-{acct}")
        return 0
    status, stats = sync_kb(sess.client("bedrock-agent"), a.kb_id, a.ds_id)
    print(f"  KB sync {status} {stats}")
    left = vector_count(v, vbucket, a.index)
    print(f"  vectors after sync          {left}")
    if status != "COMPLETE" or left:
        print("NOT clean: re-run the sync in the Bedrock console (Knowledge base > data source > Sync) and check again.")
        return 1
    print("CLEAN: S3 data, registry, trail and vectors are empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
