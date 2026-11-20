"""SQS dispatcher Lambda. One document at a time: the gate, not queue ordering, enforces single-flight.

Invoked by an EventBridge schedule (safety net) and, asynchronously, by the workflows when they release the gate or
enqueue work. Concurrent invocations are safe: the gate acquire is a conditional write, and a loser returns its message.

Per run: (1) inspect the gate (reaping a finished or dead holder), (2) receive one message, (3) validate it and the
registry state, (4) acquire the gate, (5) start the file workflow (name {document_id}-a{attempt}, so a repeated start
is idempotent), (6) delete the message only after the workflow is started and recorded.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

from cleandocs_shared.gate import Gate

REQUIRED = ("document_id", "ingestion_id", "trace_id")
# Registry statuses that mean the document must not be (re)started.
SKIP_STATUSES = {"COMPLETED", "DUPLICATE", "REJECTED", "FAILED", "PARTIAL"}


class Dispatcher:
    def __init__(self, *, sqs: Any, sfn: Any, table: Any, queue_url: str, state_machine_arn: str,
                 lease_seconds: int = 3600, clock=time.time) -> None:
        self.sqs, self.sfn, self.table = sqs, sfn, table
        self.queue_url, self.sm_arn = queue_url, state_machine_arn
        self.lease_seconds, self.clock = lease_seconds, clock
        self.gate = Gate(table)

    @classmethod
    def from_env(cls) -> "Dispatcher":
        region = os.environ.get("AWS_REGION", "eu-west-1")
        return cls(sqs=boto3.client("sqs", region_name=region), sfn=boto3.client("stepfunctions", region_name=region),
                   table=boto3.resource("dynamodb", region_name=region).Table(os.environ["REGISTRY_TABLE"]),
                   queue_url=os.environ["QUEUE_URL"], state_machine_arn=os.environ["STATE_MACHINE_ARN"],
                   lease_seconds=int(os.environ.get("LEASE_SECONDS", "3600")))

    # ---- gate inspection / reaper ----
    def _gate_is_busy(self) -> bool:
        now = int(self.clock())
        item = self.gate.read()
        if not item or "holder_document_id" not in item:
            return False
        holder, arn = item["holder_document_id"], item.get("workflow_arn")
        expired = int(item.get("lease_expires_at", 0)) < now
        if arn:
            try:
                status = self.sfn.describe_execution(executionArn=arn)["status"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("ExecutionDoesNotExist", "InvalidArn"):
                    raise
                status = "MISSING"
            if status == "RUNNING":
                if expired:  # still working past its lease: extend rather than take over
                    self.gate.extend(holder, self.lease_seconds, now)
                return True
            self.gate.release(holder)  # finished (or vanished) without releasing the gate
            return False
        if expired:  # holder never recorded a workflow and its lease ran out
            self.gate.release(holder)
            return False
        return True

    # ---- helpers ----
    def _parse(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        try:
            body = json.loads(msg["Body"])
        except (ValueError, KeyError):
            return None
        if not isinstance(body, dict) or any(not body.get(k) for k in REQUIRED):
            return None
        body.setdefault("attempt", 1)
        return body

    def _doc(self, document_id: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "META"}, ConsistentRead=True).get("Item")

    def _return(self, msg: dict[str, Any]) -> None:
        self.sqs.change_message_visibility(QueueUrl=self.queue_url, ReceiptHandle=msg["ReceiptHandle"], VisibilityTimeout=0)

    def _execution_arn(self, name: str) -> str:
        parts = self.sm_arn.split(":")  # arn:aws:states:region:acct:stateMachine:NAME
        return ":".join(parts[:5] + ["execution", parts[6], name])

    def run_once(self) -> dict[str, Any]:
        if self._gate_is_busy():
            return {"action": "busy"}
        resp = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=1, VisibilityTimeout=60, WaitTimeSeconds=0)
        messages = resp.get("Messages", [])
        if not messages:
            return {"action": "idle"}
        msg = messages[0]
        body = self._parse(msg)
        if body is None:  # not deleted: it reappears and reaches the DLQ after maxReceiveCount
            return {"action": "invalid_message", "message_id": msg.get("MessageId")}
        doc_id = body["document_id"]
        doc = self._doc(doc_id)
        if doc is None:
            return {"action": "unknown_document", "document_id": doc_id}
        if doc.get("status") in SKIP_STATUSES:
            self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=msg["ReceiptHandle"])
            return {"action": "skipped_terminal", "document_id": doc_id, "status": doc["status"]}

        if self.gate.acquire(doc_id, self.lease_seconds, int(self.clock())) is None:
            self._return(msg)
            return {"action": "busy"}
        name = f"{doc_id}-a{body['attempt']}"
        payload = {"document_id": doc_id, "ingestion_id": body["ingestion_id"], "trace_id": body["trace_id"],
                   "parent_document_id": body.get("parent_document_id"), "s3_uri": body.get("s3_uri"),
                   "checksum_sha256": body.get("checksum"), "attempt": int(body["attempt"])}
        try:
            try:
                arn = self.sfn.start_execution(stateMachineArn=self.sm_arn, name=name, input=json.dumps(payload))["executionArn"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ExecutionAlreadyExists":
                    raise
                arn = self._execution_arn(name)
            self.gate.set_workflow(doc_id, arn)
            self.table.update_item(
                Key={"PK": f"DOC#{doc_id}", "SK": "META"},
                UpdateExpression="SET workflow_execution_arn = :a, dispatched_at = :t",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":a": arn, ":t": int(self.clock())},
            )
        except Exception:
            self.gate.release(doc_id)
            self._return(msg)
            raise
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=msg["ReceiptHandle"])
        return {"action": "started", "document_id": doc_id, "execution_arn": arn}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    result = Dispatcher.from_env().run_once()
    print(json.dumps(result))
    return result
