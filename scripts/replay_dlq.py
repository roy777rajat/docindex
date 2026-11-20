"""Operator tool: re-queue documents whose file workflow failed (replay records in the DLQ).

  python scripts/replay_dlq.py                 # dry run: shows what would be re-queued
  python scripts/replay_dlq.py --apply         # re-queue FAILED documents, delete their DLQ records
Uses your default AWS credentials. Fix the cause first (see the failed_stage in the output and the audit trail).

What replay does: sets the document and its ingestion member back to PROCESSING and sends a new file message (attempt + 1).
Stages whose output already exists are skipped, so only the failed stage and later ones run again.
What it does NOT do: indexing. A replayed document stops at INDEX_READY; to publish it, run the indexing and reconciliation
calls (see docs/operations.md, "After a replay").
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import boto3

MAX_ATTEMPT = 5


def drain(sqs: Any, dlq_url: str, limit: int = 200) -> list[dict[str, Any]]:
    """Read DLQ messages without deleting them (they become visible again after the visibility timeout)."""
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        resp = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10, WaitTimeSeconds=1, VisibilityTimeout=120)
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        out.extend(msgs)
    return out


def plan(table: Any, msg: dict[str, Any], max_attempt: int = MAX_ATTEMPT) -> dict[str, Any]:
    """Decide what to do with one DLQ message. Returns {action, reason, file_message?, doc?}."""
    try:
        body = json.loads(msg["Body"])
    except ValueError:
        return {"action": "skip", "reason": "message body is not JSON"}
    did, ing = body.get("document_id"), body.get("ingestion_id")
    if not did or not ing:
        return {"action": "skip", "reason": "message has no document_id/ingestion_id"}
    doc = table.get_item(Key={"PK": f"DOC#{did}", "SK": "META"}, ConsistentRead=True).get("Item")
    ingestion = table.get_item(Key={"PK": f"INGEST#{ing}", "SK": "META"}).get("Item")
    if not doc or not ingestion:
        return {"action": "skip", "reason": "document or ingestion no longer exists", "document_id": did}
    if doc.get("status") != "FAILED":
        return {"action": "skip", "reason": f"document is {doc.get('status')}, not FAILED", "document_id": did}
    attempt = int(body.get("attempt", 1)) + 1
    if attempt > max_attempt:
        return {"action": "skip", "reason": f"already tried {attempt - 1} times", "document_id": did}
    file_message = {"document_id": did, "ingestion_id": ing, "parent_document_id": doc.get("parent_document_id"),
                    "s3_uri": doc["s3_uri_original"], "checksum": doc["checksum_sha256"], "attempt": attempt,
                    "trace_id": ingestion.get("trace_id") or body.get("trace_id") or "replay"}
    return {"action": "requeue", "document_id": did, "failed_stage": body.get("failed_stage"), "attempt": attempt,
            "file_message": file_message, "ingestion_id": ing}


def apply(table: Any, sqs: Any, queue_url: str, dlq_url: str, msg: dict[str, Any], p: dict[str, Any]) -> None:
    did, ing = p["document_id"], p["ingestion_id"]
    table.update_item(Key={"PK": f"DOC#{did}", "SK": "META"}, UpdateExpression="SET #s = :p REMOVE error_code, error_message",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":p": "PROCESSING"})
    table.update_item(Key={"PK": f"INGEST#{ing}", "SK": f"DOC#{did}"}, UpdateExpression="SET #s = :p REMOVE error_code",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":p": "PROCESSING"})
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({k: v for k, v in p["file_message"].items() if v is not None}))
    sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"])  # only after the new message is sent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--max-attempt", type=int, default=MAX_ATTEMPT)
    a = ap.parse_args()
    cf = boto3.client("cloudformation", region_name=a.region)
    out = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName="cleandocs-core")["Stacks"][0]["Outputs"]}
    sqs = boto3.client("sqs", region_name=a.region)
    table = boto3.resource("dynamodb", region_name=a.region).Table(out["RegistryTableName"])
    msgs = drain(sqs, out["FileDlqUrl"])
    print(f"{len(msgs)} message(s) in the DLQ")
    for m in msgs:
        p = plan(table, m, a.max_attempt)
        if p["action"] == "requeue":
            print(f"  requeue {p['document_id']} (failed at {p['failed_stage']}, attempt {p['attempt']})")
            if a.apply:
                apply(table, sqs, out["FileQueueUrl"], out["FileDlqUrl"], m, p)
        else:
            print(f"  skip    {p.get('document_id', '-')}: {p['reason']}")
    if not a.apply:
        print("dry run; add --apply to re-queue")


if __name__ == "__main__":
    main()
