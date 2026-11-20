"""End-to-end smoke test of the indexing path against REAL AWS (costs a fraction of a cent).

Validates the Phase 0 design assumptions:
  - one object in kb-source/ = one chunk (chunking strategy NONE)
  - the .metadata.json sidecar is accepted
  - a KB ingestion job indexes it into the S3 Vectors index
Then removes the test object and re-syncs so the vector is deleted again.

Usage (PowerShell, default AWS credentials):  python scripts/kb_smoke_test.py
"""
import json
import sys
import time

import boto3

REGION = "eu-west-1"
KB_ID = "NEYHRDRGOT"
DS_ID = "FWC2ZGVLIT"
VECTOR_BUCKET = None  # resolved from account id below
INDEX = "cleandocs-index"

sts = boto3.client("sts", region_name=REGION)
account = sts.get_caller_identity()["Account"]
BUCKET = f"cleandocs-{account}-{REGION}"
VECTOR_BUCKET = f"cleandocs-vectors-{account}"

s3 = boto3.client("s3", region_name=REGION)
agent = boto3.client("bedrock-agent", region_name=REGION)
vectors = boto3.client("s3vectors", region_name=REGION)

KEY = "kb-source/doc_smoketest/chunk_0001.txt"
META_KEY = KEY + ".metadata.json"


def run_job() -> dict:
    job = agent.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID)["ingestionJob"]
    jid = job["ingestionJobId"]
    while True:
        job = agent.get_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID, ingestionJobId=jid)["ingestionJob"]
        if job["status"] in ("COMPLETE", "FAILED", "STOPPED"):
            return job
        time.sleep(5)


def count_vectors() -> int:
    n = 0
    for page in vectors.get_paginator("list_vectors").paginate(vectorBucketName=VECTOR_BUCKET, indexName=INDEX):
        n += len(page.get("vectors", []))
    return n


before = count_vectors()
s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"Smoke test chunk. Invoice total 100.00 EUR dated 2026-01-01.")
s3.put_object(
    Bucket=BUCKET,
    Key=META_KEY,
    Body=json.dumps({"metadataAttributes": {"document_id": "doc_smoketest", "chunk_id": "chunk_0001", "category": "invoice"}}).encode(),
)

job = run_job()
print("ingest 1:", job["status"], job.get("statistics"))
after = count_vectors()
print(f"vectors before={before} after={after}")
ok = job["status"] == "COMPLETE" and after == before + 1

s3.delete_object(Bucket=BUCKET, Key=KEY)
s3.delete_object(Bucket=BUCKET, Key=META_KEY)
job2 = run_job()
print("ingest 2 (cleanup):", job2["status"], job2.get("statistics"))
print("vectors after cleanup:", count_vectors())

print("SMOKE TEST", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
