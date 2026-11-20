import pytest
from boto3.dynamodb.conditions import Key

from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.exceptions import CleanDocsError, ValidationFailure
from cleandocs_shared.models import ExecutionStatus, TraceContext


def _writer(table):
    return AuditWriter(table, "validation-service", "0.1.0", render_instance_id="srv-1")


def _ctx():
    return TraceContext(document_id="doc_1", ingestion_id="ing_1", trace_id="t1", workflow_execution_id="wf1")


def _rows(table, pk="DOCUMENT#doc_1"):
    return table.query(KeyConditionExpression=Key("PK").eq(pk))["Items"]


def test_success_writes_started_and_completed_rows(trail_table):
    with _writer(trail_table).execution(_ctx(), input_s3_uri="s3://b/k", input_checksum="a" * 64) as rec:
        rec.set_output("s3://b/out", "b" * 64)
        rec.add_metric("pages", 3)
    rows = _rows(trail_table)
    assert [r["status"] for r in rows] == ["STARTED", "COMPLETED"]
    done = rows[1]
    assert done["service_name"] == "validation-service" and done["service_version"] == "0.1.0"
    assert done["output_s3_uri"] == "s3://b/out" and done["metrics"]["pages"] == 3
    assert done["trace_id"] == "t1" and done["workflow_execution_id"] == "wf1"
    assert done["render_instance_id"] == "srv-1" and done["duration_ms"] >= 0
    assert rows[0]["service_execution_id"] == done["service_execution_id"]
    assert done["service_execution_id"].startswith("validation-service_")


def test_failure_writes_failed_row_with_classification_and_reraises(trail_table):
    with pytest.raises(ValidationFailure):
        with _writer(trail_table).execution(_ctx()) as rec:
            raise ValidationFailure("bad magic bytes", code="MAGIC_MISMATCH")
    failed = _rows(trail_table)[-1]
    assert failed["status"] == "FAILED" and failed["error_code"] == "MAGIC_MISMATCH"
    assert failed["error_class"] == "VALIDATION" and failed["retryable"] is False


def test_unknown_exception_is_recorded_without_its_message(trail_table):
    with pytest.raises(CleanDocsError):
        with _writer(trail_table).execution(_ctx()):
            raise ValueError("SSN 123-45-6789")
    failed = _rows(trail_table)[-1]
    assert failed["status"] == "FAILED" and "123-45" not in failed["error_message"]


def test_skip_and_not_implemented_statuses(trail_table):
    with _writer(trail_table).execution(_ctx()) as rec:
        rec.skip("output exists with matching checksum")
    with _writer(trail_table).execution(_ctx()) as rec:
        rec.not_implemented()
    statuses = [r["status"] for r in _rows(trail_table)]
    assert ExecutionStatus.SKIPPED.value in statuses and ExecutionStatus.NOT_IMPLEMENTED.value in statuses


def test_retry_creates_new_execution_and_retried_row_without_touching_old(trail_table):
    w = _writer(trail_table)
    with pytest.raises(ValidationFailure):
        with w.execution(_ctx()):
            raise ValidationFailure("x")
    first_failed = [r for r in _rows(trail_table) if r["status"] == "FAILED"][0]
    with w.execution(_ctx(), attempt=2, retry_of=first_failed["service_execution_id"], retry_reason="transient") as rec:
        pass
    rows = _rows(trail_table)
    assert len({r["service_execution_id"] for r in rows}) == 2
    retried = [r for r in rows if r["status"] == "RETRIED"][0]
    assert retried["retry_of"] == first_failed["service_execution_id"] and retried["attempt_number"] == 2
    assert [r for r in rows if r["SK"] == first_failed["SK"]][0] == first_failed  # unchanged


def test_records_are_immutable(trail_table):
    w = _writer(trail_table)
    item = {"PK": "DOCUMENT#doc_1", "SK": "EXEC#x#y#STARTED", "status": "STARTED"}
    w._put(item)
    with pytest.raises(CleanDocsError) as e:
        w._put(item)
    assert e.value.code == "AUDIT_IMMUTABLE"


def test_ingestion_level_records_use_ingestion_partition(trail_table):
    ctx = TraceContext(ingestion_id="ing_9", trace_id="t")
    with _writer(trail_table).execution(ctx):
        pass
    assert len(_rows(trail_table, "INGESTION#ing_9")) == 2
