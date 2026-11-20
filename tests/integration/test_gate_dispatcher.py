import json
import sys
from pathlib import Path

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers" / "sqs-dispatcher"))

from cleandocs_shared.gate import Gate  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402
from handler import Dispatcher  # noqa: E402

DEFN = json.dumps({"StartAt": "A", "States": {"A": {"Type": "Pass", "End": True}}})


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(aws_env):
    sqs = boto3.client("sqs", region_name="eu-west-1")
    q = sqs.create_queue(QueueName="q", Attributes={"VisibilityTimeout": "60"})["QueueUrl"]
    sfn = aws_env["sfn"]
    sm = sfn.create_state_machine(name="file-wf", definition=DEFN, roleArn="arn:aws:iam::123456789012:role/x")["stateMachineArn"]
    clock = Clock()
    table = aws_env["registry_table"]
    d = Dispatcher(sqs=sqs, sfn=sfn, table=table, queue_url=q, state_machine_arn=sm, lease_seconds=600, clock=clock)
    return {"d": d, "sqs": sqs, "q": q, "sfn": sfn, "sm": sm, "clock": clock, "table": table, "reg": Registry(table)}


def add_doc(env, doc_id, status="PROCESSING"):
    reg = env["reg"]
    if reg.get_ingestion("ing_1") is None:
        reg.create_ingestion(ingestion_id="ing_1", trace_id="t")
    reg.create_document(document_id=doc_id, ingestion_id="ing_1", original_filename="a.pdf", extension="pdf", size_bytes=1,
                        checksum_sha256="a" * 64, s3_uri_original="s3://b/x")
    if status != "PROCESSING":
        reg.update_document(doc_id, status=status)


def enqueue(env, doc_id, attempt=1, body=None):
    msg = body or {"document_id": doc_id, "ingestion_id": "ing_1", "trace_id": "t", "attempt": attempt, "s3_uri": "s3://b/x", "checksum": "a" * 64}
    env["sqs"].send_message(QueueUrl=env["q"], MessageBody=json.dumps(msg))


def depth(env):
    a = env["sqs"].get_queue_attributes(QueueUrl=env["q"], AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


# ---------------- gate ----------------
def test_gate_single_holder_with_fence_and_lease_expiry(env):
    g = Gate(env["table"])
    assert g.acquire("a", 100, now=1000) == 1
    assert g.acquire("b", 100, now=1050) is None            # live lease blocks others
    assert g.acquire("b", 100, now=1101) == 2               # expired lease can be taken; fence increases
    assert g.release("a") is False                          # the old holder can no longer release
    assert Gate.holder(g.read(), 1102) == "b"
    assert g.extend("b", 100, now=1150) and g.read()["lease_expires_at"] == 1250
    assert g.release("b") is True and Gate.holder(g.read(), 1151) is None
    assert g.release("b") is False                          # already released
    assert g.acquire("c", 10, now=1200) == 3


# ---------------- dispatcher ----------------
def test_starts_workflow_records_arn_and_deletes_message(env):
    add_doc(env, "doc_a")
    enqueue(env, "doc_a")
    r = env["d"].run_once()
    assert r["action"] == "started" and r["execution_arn"].endswith(":execution:file-wf:doc_a-a1")
    assert depth(env) == 0
    assert env["reg"].get_document("doc_a")["workflow_execution_arn"] == r["execution_arn"]
    item = Gate(env["table"]).read()
    assert item["holder_document_id"] == "doc_a" and item["workflow_arn"] == r["execution_arn"]


def test_single_flight_busy_gate_does_not_receive(env):
    add_doc(env, "doc_a"); add_doc(env, "doc_b")
    enqueue(env, "doc_a"); enqueue(env, "doc_b")
    assert env["d"].run_once()["action"] == "started"
    assert env["d"].run_once()["action"] == "busy"          # holder's execution is RUNNING: second doc waits
    assert depth(env) == 1
    env["clock"].t += 1000                                   # lease expired but workflow still running: extended, not stolen
    assert env["d"].run_once()["action"] == "busy" and Gate(env["table"]).read()["holder_document_id"] == "doc_a"


def test_finished_holder_is_reaped_and_next_document_starts(env):
    add_doc(env, "doc_a"); add_doc(env, "doc_b")
    enqueue(env, "doc_a"); enqueue(env, "doc_b")
    first = env["d"].run_once()
    env["sfn"].stop_execution(executionArn=first["execution_arn"])  # finished without releasing the gate (e.g. crash)
    r = env["d"].run_once()
    assert r["action"] == "started" and r["document_id"] == "doc_b"


def test_lost_race_returns_message_to_queue(env):
    add_doc(env, "doc_a")
    enqueue(env, "doc_a")
    Gate(env["table"]).acquire("someone_else", 600, now=int(env["clock"]()))
    real = env["d"]._gate_is_busy
    env["d"]._gate_is_busy = lambda: False                   # both dispatchers saw a free gate; one lost the acquire
    r = env["d"].run_once()
    env["d"]._gate_is_busy = real
    assert r["action"] == "busy" and depth(env) == 1
    assert env["d"].run_once()["action"] == "busy"           # message is receivable again once the gate frees


def test_terminal_unknown_and_invalid_messages(env):
    add_doc(env, "doc_dup", status="DUPLICATE")
    enqueue(env, "doc_dup")
    assert env["d"].run_once()["action"] == "skipped_terminal" and depth(env) == 0   # stale message is dropped
    enqueue(env, "ghost")
    assert env["d"].run_once()["action"] == "unknown_document" and depth(env) == 1   # kept: heads to the DLQ if it persists
    env["sqs"].purge_queue(QueueUrl=env["q"])
    enqueue(env, "x", body={"document_id": "x"})
    assert env["d"].run_once()["action"] == "invalid_message"
    assert env["d"].run_once()["action"] in ("idle", "invalid_message")


def test_duplicate_start_is_idempotent_and_failure_releases_gate(env, monkeypatch):
    add_doc(env, "doc_a")
    enqueue(env, "doc_a")
    r1 = env["d"].run_once()
    env["sfn"].stop_execution(executionArn=r1["execution_arn"])
    env["reg"].update_document("doc_a", stage="VALIDATED")
    enqueue(env, "doc_a")                                    # redelivery of the same attempt
    r2 = env["d"].run_once()
    assert r2["action"] == "started" and r2["execution_arn"] == r1["execution_arn"]  # same execution name, no second run
    env["sfn"].stop_execution(executionArn=r2["execution_arn"])
    add_doc(env, "doc_b"); enqueue(env, "doc_b")

    def boom(**kw):
        raise RuntimeError("sfn down")

    monkeypatch.setattr(env["d"].sfn, "start_execution", boom)
    with pytest.raises(RuntimeError):
        env["d"].run_once()
    assert Gate.holder(Gate(env["table"]).read(), int(env["clock"]())) is None and depth(env) == 1
