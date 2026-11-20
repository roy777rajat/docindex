import json
import sys
from pathlib import Path

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import replay_dlq  # noqa: E402
from cleandocs_shared.registry import Registry  # noqa: E402


@pytest.fixture
def q(aws_env):
    sqs = boto3.client("sqs", region_name="eu-west-1")
    return sqs, sqs.create_queue(QueueName="files")["QueueUrl"], sqs.create_queue(QueueName="dlq")["QueueUrl"]


def setup_doc(aws_env, status="FAILED"):
    reg = Registry(aws_env["registry_table"])
    reg.create_ingestion(ingestion_id="i1", trace_id="tr1")
    reg.create_document(document_id="d1", ingestion_id="i1", original_filename="a.txt", extension="txt", size_bytes=3,
                        checksum_sha256="a" * 64, s3_uri_original="s3://b/raw/a.txt")
    reg.add_member(ingestion_id="i1", document_id="d1")
    reg.update_document("d1", status=status, error_code="X", error_message="boom")
    reg.update_member("i1", "d1", status=status, error_code="X")
    return reg


def dlq_record(sqs, url, **over):
    body = {"type": "workflow_failure", "document_id": "d1", "ingestion_id": "i1", "failed_stage": "extraction", "attempt": 1, **over}
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(body))


def test_failed_document_is_requeued_with_next_attempt_and_dlq_record_removed(aws_env, q):
    sqs, queue, dlq = q
    reg = setup_doc(aws_env)
    dlq_record(sqs, dlq)
    [msg] = replay_dlq.drain(sqs, dlq)
    p = replay_dlq.plan(aws_env["registry_table"], msg)
    assert p["action"] == "requeue" and p["attempt"] == 2 and p["failed_stage"] == "extraction"
    replay_dlq.apply(aws_env["registry_table"], sqs, queue, dlq, msg, p)
    sent = json.loads(sqs.receive_message(QueueUrl=queue)["Messages"][0]["Body"])
    assert sent == {"document_id": "d1", "ingestion_id": "i1", "s3_uri": "s3://b/raw/a.txt", "checksum": "a" * 64, "attempt": 2,
                    "trace_id": "tr1"}
    d = reg.get_document("d1")
    assert d["status"] == "PROCESSING" and "error_code" not in d
    assert reg.list_members("i1")[0]["status"] == "PROCESSING"
    assert "Messages" not in sqs.receive_message(QueueUrl=dlq, VisibilityTimeout=0)


@pytest.mark.parametrize("status,over,reason", [("COMPLETED", {}, "not FAILED"), ("FAILED", {"attempt": 5}, "already tried"),
                                                  ("FAILED", {"document_id": "nope"}, "no longer exists")])
def test_documents_that_should_not_be_replayed_are_skipped(aws_env, q, status, over, reason):
    sqs, _queue, dlq = q
    setup_doc(aws_env, status)
    dlq_record(sqs, dlq, **over)
    [msg] = replay_dlq.drain(sqs, dlq)
    p = replay_dlq.plan(aws_env["registry_table"], msg)
    assert p["action"] == "skip" and reason in p["reason"]


def test_garbage_message_is_skipped(aws_env, q):
    sqs, _queue, dlq = q
    sqs.send_message(QueueUrl=dlq, MessageBody="not json")
    [msg] = replay_dlq.drain(sqs, dlq)
    assert replay_dlq.plan(aws_env["registry_table"], msg)["action"] == "skip"
