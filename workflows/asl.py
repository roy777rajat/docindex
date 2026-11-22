"""Generates the two Step Functions (Standard) definitions as JSON.

Why generated: every Render service call is the same two-state pattern (readiness GET with retries, then POST), and a
hand-written definition of ~100 states would drift. Account-specific values use ${Placeholders} that CloudFormation
fills via DefinitionSubstitutions; service URLs are literal and come from --url-template at build time.

Notes on limits (checked against AWS docs): an HTTP Task cannot run longer than 60 s, so every service call is bounded
(OCR is batched) and long waits are Wait+Choice loops; state input/output is capped at 256 KiB, so each stage keeps only
its small `status` and `result` summary.
"""
from __future__ import annotations

from typing import Any

HTTP = "arn:aws:states:::http:invoke"
BUSINESS = [f"States.Http.StatusCode.{c}" for c in (404, 409, 415, 422)]
TRANSIENT = ["States.Timeout"] + [f"States.Http.StatusCode.{c}" for c in (429, 502, 503, 504)]
CONN = {"ConnectionArn": "${ConnectionArn}"}

FILE_STAGES = ["validation", "normalization", "cleaning", "duplicate", "extraction", "metadata", "chunking", "embedding"]
OPTIONAL_STAGES = ["entity-extraction", "graph"]


def _retry(interval: int, backoff: float, attempts: int, errors: list[str] | None = None) -> dict[str, Any]:
    return {"ErrorEquals": errors or ["States.ALL"], "IntervalSeconds": interval, "BackoffRate": backoff,
            "MaxAttempts": attempts, "JitterStrategy": "FULL"}


def _label(service: str) -> str:
    return "".join(p.capitalize() for p in service.split("-"))


class Builder:
    def __init__(self, urls: dict[str, str]) -> None:
        self.urls = urls
        self.states: dict[str, Any] = {}

    def add(self, name: str, state: dict[str, Any]) -> str:
        assert name not in self.states, f"duplicate state {name}"
        self.states[name] = state
        return name

    # ---- Render service calls ----
    def ready(self, service: str, next_: str, catch_to: str | None, attempts: int = 8, name: str | None = None) -> str:
        name = name or f"{_label(service)}Ready"
        state: dict[str, Any] = {
            "Type": "Task", "Resource": HTTP, "TimeoutSeconds": 60, "ResultPath": None,
            "Parameters": {"ApiEndpoint": f"{self.urls[service]}/health/ready", "Method": "GET", "Authentication": CONN},
            # a sleeping free instance needs about a minute; this retry budget (~12 min at 8 attempts) absorbs it
            "Retry": [_retry(15, 1.5, attempts)], "Next": next_,
        }
        if catch_to:
            state["Catch"] = [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": catch_to}]
        return self.add(name, state)

    def call(self, service: str, stage: str, next_: str, *, context: dict[str, str], parameters: dict[str, Any] | None = None,
             on_business: str | None, on_error: str, path: str = "/api/v1/process", method: str = "POST",
             retry: dict[str, Any] | None = None, result_path: str | None = None, selector: dict[str, str] | None = None,
             name: str | None = None, endpoint_format: tuple[str, list[str]] | None = None, body: bool = True,
             key_path: str = "$.document_id") -> str:
        params: dict[str, Any] = {"Method": method, "Authentication": CONN,
                                  "Headers": {"Content-Type": "application/json", "X-Trace-Id.$": "$.trace_id",
                                              "Idempotency-Key.$": f"States.Format('{{}}-{stage}', {key_path})"}}
        if endpoint_format:
            fmt, args = endpoint_format
            params["ApiEndpoint.$"] = f"States.Format('{fmt}', {', '.join(args)})"
        else:
            params["ApiEndpoint"] = f"{self.urls[service]}{path}"
        if body and method == "POST":
            params["RequestBody"] = {"context": context, "parameters": parameters or {}}
        state: dict[str, Any] = {
            "Type": "Task", "Resource": HTTP, "TimeoutSeconds": 60, "Parameters": params,
            "ResultSelector": selector or {"status.$": "$.ResponseBody.status", "result.$": "$.ResponseBody.result"},
            "ResultPath": result_path or f"$.stages.{stage}",
            "Retry": [retry or _retry(10, 2.0, 5, TRANSIENT)], "Next": next_,
        }
        catch = []
        if on_business:
            catch.append({"ErrorEquals": BUSINESS, "ResultPath": "$.error", "Next": on_business})
        catch.append({"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": on_error})
        state["Catch"] = catch
        return self.add(name or _label(stage), state)


def file_workflow(urls: dict[str, str], duplicate_policy_ref: str = "${DuplicatePolicy}") -> dict[str, Any]:
    b = Builder(urls)
    ctx = {"document_id.$": "$.document_id", "ingestion_id.$": "$.ingestion_id", "parent_document_id.$": "$.parent_document_id",
           "trace_id.$": "$.trace_id", "workflow_execution_id.$": "$$.Execution.Id"}

    # ---- terminal / cleanup states ----
    b.add("ReleaseGate", {
        "Type": "Task", "Resource": "arn:aws:states:::dynamodb:updateItem", "ResultPath": None,
        "Parameters": {"TableName": "${RegistryTable}", "Key": {"PK": {"S": "GATE#GLOBAL"}, "SK": {"S": "LOCK"}},
                       "UpdateExpression": "REMOVE holder_document_id, workflow_arn, lease_expires_at",
                       "ConditionExpression": "holder_document_id = :d",
                       "ExpressionAttributeValues": {":d": {"S.$": "$.document_id"}}},
        "Catch": [{"ErrorEquals": ["DynamoDB.ConditionalCheckFailedException"], "ResultPath": None, "Next": "NudgeDispatcher"},
                  {"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "NudgeDispatcher"}],  # lease expiry is the backstop
        "Next": "NudgeDispatcher"})
    b.add("NudgeDispatcher", {
        "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "ResultPath": None,
        "Parameters": {"FunctionName": "${DispatcherArn}", "InvocationType": "Event", "Payload": {"source": "file-workflow"}},
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "Done"}],  # the schedule is the fallback
        "Next": "Done"})
    b.add("Done", {"Type": "Succeed"})
    # expected rejections (4xx): the service already recorded the reason; free the gate and end quietly
    b.add("BusinessFailure", {"Type": "Pass", "Next": "ReleaseGate"})
    # unexpected failures: mark the member, leave a replay record in the DLQ, free the gate, fail the execution
    b.add("MarkDocFailed", {
        "Type": "Task", "Resource": HTTP, "TimeoutSeconds": 60, "ResultPath": None,
        "Parameters": {"ApiEndpoint.$": f"States.Format('{urls['manifest']}/api/v1/ingestions/{{}}/documents/{{}}/state', $.ingestion_id, $.document_id)",
                       "Method": "POST", "Authentication": CONN, "Headers": {"Content-Type": "application/json"},
                       "RequestBody": {"status": "FAILED", "error_code": "WORKFLOW_ERROR"}},
        "Retry": [_retry(15, 1.5, 6)], "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "WriteReplayRecord"}],
        "Next": "WriteReplayRecord"})
    b.add("WriteReplayRecord", {
        "Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage", "ResultPath": None,
        "Parameters": {"QueueUrl": "${DlqUrl}", "MessageBody": {
            "type": "workflow_failure", "document_id.$": "$.document_id", "ingestion_id.$": "$.ingestion_id",
            "failed_stage.$": "$.failed_stage", "attempt.$": "$.attempt", "execution_id.$": "$$.Execution.Id",
            "error.$": "$.error"}},
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "ReleaseGateAfterFailure"}],
        "Next": "ReleaseGateAfterFailure"})
    b.add("ReleaseGateAfterFailure", {
        "Type": "Task", "Resource": "arn:aws:states:::dynamodb:updateItem", "ResultPath": None,
        "Parameters": b.states["ReleaseGate"]["Parameters"],
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "NudgeAfterFailure"}], "Next": "NudgeAfterFailure"})
    b.add("NudgeAfterFailure", {
        "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "ResultPath": None,
        "Parameters": b.states["NudgeDispatcher"]["Parameters"],
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "WorkflowFailed"}], "Next": "WorkflowFailed"})
    b.add("WorkflowFailed", {"Type": "Fail", "Error": "FileWorkflowFailed", "Cause": "a stage failed; see the audit trail and DLQ replay record"})

    def fail_state(stage: str) -> str:
        return b.add(f"Failed{_label(stage)}", {"Type": "Pass", "Result": stage, "ResultPath": "$.failed_stage", "Next": "MarkDocFailed"})

    # ---- optional branch (never fails the document) ----
    nxt = "ReleaseGate"
    for stage in reversed(OPTIONAL_STAGES):
        proc = b.call(stage, stage, nxt, context=ctx, on_business=nxt, on_error=nxt)
        b.states[proc]["Catch"] = [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.optional_error", "Next": nxt}]
        rdy = b.ready(stage, proc, None, attempts=5)
        b.states[rdy]["Catch"] = [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.optional_error", "Next": nxt}]
        nxt = rdy
    optional_start = nxt

    # ---- required stages, built back to front ----
    nxt = optional_start
    for stage in reversed(FILE_STAGES):
        svc = stage
        params = {"duplicate_policy": duplicate_policy_ref} if stage == "duplicate" else None
        fail = fail_state(stage)
        proc_next = nxt
        if stage == "duplicate":
            b.add("IsDuplicate", {"Type": "Choice", "Choices": [
                {"Variable": "$.stages.duplicate.result.skip_remaining", "BooleanEquals": True, "Next": "ReleaseGate"}],
                "Default": nxt})
            proc_next = "IsDuplicate"
        if stage == "extraction":
            b.add("ExtractionDone", {"Type": "Choice", "Choices": [
                {"And": [{"Variable": "$.stages.extraction.result.complete", "IsPresent": True},
                         {"Variable": "$.stages.extraction.result.complete", "BooleanEquals": False}], "Next": "Extraction"}],
                "Default": nxt})
            proc_next = "ExtractionDone"
        proc = b.call(svc, stage, proc_next, context=ctx, parameters=params, on_business="BusinessFailure", on_error=fail)
        nxt = b.ready(svc, proc, fail)
    return {"Comment": "CleanDocs file workflow (one document at a time, guarded by the DynamoDB gate)",
            "StartAt": nxt, "States": b.states}


def ingestion_workflow(urls: dict[str, str], max_settle_polls: int = 240, max_index_polls: int = 120) -> dict[str, Any]:
    b = Builder(urls)
    ctx_ing = {"ingestion_id.$": "$.ingestion_id", "trace_id.$": "$.trace_id", "workflow_execution_id.$": "$$.Execution.Id"}
    ctx_doc = {"document_id.$": "$.document_id", **ctx_ing}
    status_selector = {"status.$": "$.ResponseBody.status", "all_settled.$": "$.ResponseBody.all_settled",
                       "index_ready_files.$": "$.ResponseBody.index_ready_files", "pending_files.$": "$.ResponseBody.pending_files",
                       "all_terminal.$": "$.ResponseBody.all_terminal", "counts.$": "$.ResponseBody.counts"}
    status_ep = (f"{urls['manifest']}/api/v1/ingestions/{{}}", ["$.ingestion_id"])

    b.add("IngestionDone", {"Type": "Succeed"})
    b.add("IngestionFailed", {"Type": "Fail", "Error": "IngestionWorkflowFailed", "Cause": "a step failed; see the registry and audit trail"})
    b.add("TimedOut", {"Type": "Fail", "Error": "IngestionTimedOut", "Cause": "documents did not settle or index within the polling cap"})
    # roll-up writes the ingestion (and archive) status; also used on every exit path
    rollup_call = b.call("manifest", "rollup", "IngestionDone", context=ctx_ing, method="GET", on_business=None, on_error="IngestionFailed",
                         result_path="$.final", selector=status_selector, endpoint_format=status_ep, name="RollUp", body=False,
                         key_path="$.ingestion_id")
    b.ready("manifest", rollup_call, "IngestionFailed", name="RollUpReady")  # every exit path rolls up first
    b.add("RollUpRejected", {"Type": "Pass", "Next": "RollUpReady"})
    b.add("MarkIngestionFailed", {
        "Type": "Task", "Resource": HTTP, "TimeoutSeconds": 60, "ResultPath": None,
        "Parameters": {"ApiEndpoint.$": f"States.Format('{urls['manifest']}/api/v1/ingestions/{{}}/documents/{{}}/state', $.ingestion_id, $.document_id)",
                       "Method": "POST", "Authentication": CONN, "Headers": {"Content-Type": "application/json"},
                       "RequestBody": {"status": "FAILED", "error_code": "WORKFLOW_ERROR"}},
        "Retry": [_retry(15, 1.5, 6)], "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "IngestionFailed"}],
        "Next": "IngestionFailed"})

    # ---- index phase ----
    # ---- reconcile: evidence-based final statuses (time-budgeted; loops while the service reports complete=false) ----
    b.add("ReconcileAfterIndex", {"Type": "Pass", "Next": "ReconciliationReady"})
    b.add("ReconcileMore", {"Type": "Choice", "Choices": [
        {"Variable": "$.reconciliation.result.complete", "BooleanEquals": False, "Next": "Reconcile"}], "Default": "RollUpReady"})
    b.call("reconciliation", "reconcile", "ReconcileMore", context=ctx_ing, on_business="RollUpReady", on_error="RollUpReady",
           result_path="$.reconciliation", retry=_retry(10, 2.0, 5, TRANSIENT), name="Reconcile", key_path="$.ingestion_id")
    b.ready("reconciliation", "Reconcile", "RollUpReady", name="ReconciliationReady")  # a reconciliation outage must not hide the roll-up
    b.add("IndexWait", {"Type": "Wait", "Seconds": 60, "Next": "IndexPoll"})
    b.add("IndexCount", {"Type": "Pass", "Parameters": {"count.$": "States.MathAdd($.index.count, 1)"}, "ResultPath": "$.index", "Next": "IndexWait"})
    b.add("IndexRunning", {"Type": "Choice", "Choices": [
        {"Variable": "$.indexing.state", "StringEquals": "RUNNING", "Next": "IndexCapCheck"}], "Default": "ReconcileAfterIndex"})
    b.add("IndexCapCheck", {"Type": "Choice", "Choices": [
        {"Variable": "$.index.count", "NumericGreaterThanEquals": max_index_polls, "Next": "TimedOut"}], "Default": "IndexCount"})
    b.call("vector-indexing", "index-poll", "IndexRunning", context={}, method="POST", on_business=None, on_error="MarkIngestionFailed",
           result_path="$.indexing", selector={"state.$": "$.ResponseBody.state"}, name="IndexPoll", body=False, key_path="$.ingestion_id",
           endpoint_format=(f"{urls['vector-indexing']}/api/v1/ingestions/{{}}/index/poll", ["$.ingestion_id"]))
    b.add("IndexInit", {"Type": "Pass", "Result": {"count": 0}, "ResultPath": "$.index", "Next": "IndexWait"})
    idx_start = b.call("vector-indexing", "index-start", "IndexInit", context=ctx_ing, on_business=None, on_error="MarkIngestionFailed",
                       retry=_retry(30, 1.5, 10, TRANSIENT), name="IndexStart", key_path="$.ingestion_id")
    idx_ready = b.ready("vector-indexing", idx_start, "MarkIngestionFailed")
    b.add("ShouldIndex", {"Type": "Choice", "Choices": [
        {"Variable": "$.settle.index_ready_files", "NumericGreaterThan": 0, "Next": idx_ready}], "Default": "ReconcileAfterIndex"})

    # ---- wait for files to settle ----
    b.add("SettleCapCheck", {"Type": "Choice", "Choices": [
        {"Variable": "$.settle_poll.count", "NumericGreaterThanEquals": max_settle_polls, "Next": "TimedOut"}], "Default": "SettleCount"})
    b.add("SettleCount", {"Type": "Pass", "Parameters": {"count.$": "States.MathAdd($.settle_poll.count, 1)"},
                          "ResultPath": "$.settle_poll", "Next": "SettleWait"})
    b.add("SettleWait", {"Type": "Wait", "Seconds": 60, "Next": "SettlePoll"})
    b.add("Settled", {"Type": "Choice", "Choices": [
        {"Variable": "$.settle.all_settled", "BooleanEquals": True, "Next": "ShouldIndex"}], "Default": "SettleCapCheck"})
    b.call("manifest", "settle", "Settled", context=ctx_ing, method="GET", on_business=None, on_error="MarkIngestionFailed",
           result_path="$.settle", selector=status_selector, endpoint_format=status_ep, name="SettlePoll", body=False, key_path="$.ingestion_id")

    # ---- publish files to the queue ----
    b.add("SettleInit", {"Type": "Pass", "Result": {"count": 0}, "ResultPath": "$.settle_poll", "Next": "SettleWait"})
    b.add("NudgeDispatcher", {
        "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "ResultPath": None,
        "Parameters": {"FunctionName": "${DispatcherArn}", "InvocationType": "Event", "Payload": {"source": "ingestion-workflow"}},
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "SettleInit"}], "Next": "SettleInit"})
    b.add("PublishFiles", {
        "Type": "Map", "ItemsPath": "$.stages.manifest.result.files", "MaxConcurrency": 1, "ResultPath": None,
        "ItemSelector": {"file.$": "$$.Map.Item.Value", "ingestion_id.$": "$.ingestion_id", "trace_id.$": "$.trace_id"},
        "ItemProcessor": {"ProcessorConfig": {"Mode": "INLINE"}, "StartAt": "SendFile", "States": {"SendFile": {
            "Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage", "End": True, "Parameters": {
                "QueueUrl": "${QueueUrl}", "MessageBody": {
                    "document_id.$": "$.file.document_id", "ingestion_id.$": "$.ingestion_id",
                    "parent_document_id.$": "$.file.parent_document_id", "s3_uri.$": "$.file.s3_uri",
                    "checksum.$": "$.file.checksum_sha256", "attempt": 1, "trace_id.$": "$.trace_id"}}}}},
        "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "MarkIngestionFailed"}],
        "Next": "NudgeDispatcher"})
    b.add("HasFiles", {"Type": "Choice", "Choices": [
        {"Variable": "$.stages.manifest.result.files[0]", "IsPresent": True, "Next": "PublishFiles"}], "Default": "RollUpReady"})
    man = b.call("manifest", "manifest", "HasFiles", context=ctx_ing, on_business="RollUpRejected", on_error="MarkIngestionFailed",
                 key_path="$.ingestion_id")
    man_ready = b.ready("manifest", man, "MarkIngestionFailed")

    # ---- validate, optionally expand an archive ----
    arc = b.call("archive", "archive", man_ready, context=ctx_doc, on_business="RollUpRejected", on_error="MarkIngestionFailed")
    arc_ready = b.ready("archive", arc, "MarkIngestionFailed")
    b.add("IsArchive", {"Type": "Choice", "Choices": [
        {"Variable": "$.stages.validation.result.is_archive", "BooleanEquals": True, "Next": arc_ready}], "Default": man_ready})
    val = b.call("validation", "validation", "IsArchive", context=ctx_doc, on_business="RollUpRejected", on_error="MarkIngestionFailed")
    start = b.ready("validation", val, "MarkIngestionFailed")
    return {"Comment": "CleanDocs ingestion workflow: validate, expand, enqueue files, wait, index, roll up",
            "StartAt": start, "States": b.states}
