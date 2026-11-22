"""Contract tests between the generated Step Functions definitions, the CloudFormation template and the services."""
import ast
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflows"))
sys.path.insert(0, str(ROOT / "scripts"))

from asl import BUSINESS, FILE_STAGES, TRANSIENT, file_workflow, ingestion_workflow  # noqa: E402
from build import SERVICES, urls_for  # noqa: E402
from cleandocs_shared.exceptions import HTTP_STATUS, FailureClass, RETRYABLE  # noqa: E402
from cleandocs_shared.models import ProcessRequest  # noqa: E402
from package_dispatcher import FILES, build  # noqa: E402
from validate import placeholders, validate  # noqa: E402

URLS = urls_for("https://cleandocs-{service}-service.onrender.com")


@pytest.fixture(scope="module")
def defs():
    return {"file": file_workflow(URLS), "ingestion": ingestion_workflow(URLS)}


def http_states(d):
    out = {}

    def walk(states):
        for n, s in states.items():
            if s["Type"] == "Task" and s["Resource"].endswith("http:invoke"):
                out[n] = s
            if s["Type"] == "Map":
                walk(s["ItemProcessor"]["States"])

    walk(d["States"])
    return out


def test_definitions_are_structurally_valid(defs):
    for name, d in defs.items():
        assert validate(d) == [], name


def test_every_http_task_is_bounded_authenticated_and_retries_transient_errors(defs):
    for name, d in defs.items():
        for n, s in http_states(d).items():
            assert s["TimeoutSeconds"] <= 60, n                       # hard AWS cap for HTTP tasks
            assert s["Parameters"]["Authentication"] == {"ConnectionArn": "${ConnectionArn}"}, n
            assert s["Retry"] and s["Catch"], n
            ep = s["Parameters"].get("ApiEndpoint") or s["Parameters"]["ApiEndpoint.$"]
            assert ep.startswith("https://") or ep.startswith("States.Format('https://"), n
        for n, s in http_states(d).items():
            if n.endswith("Ready"):
                assert s["Parameters"]["Method"] == "GET" and s["Parameters"]["ApiEndpoint"].endswith("/health/ready")


def test_retry_and_business_error_classes_match_the_services_http_mapping():
    retry_codes = {int(e.rsplit(".", 1)[1]) for e in TRANSIENT if e.startswith("States.Http")}
    business = {int(e.rsplit(".", 1)[1]) for e in BUSINESS}
    for cls, status in HTTP_STATUS.items():
        if cls in RETRYABLE:
            assert status in retry_codes, cls                         # every retryable service error is retried
        elif cls is not FailureClass.INTERNAL:
            assert status in business, cls                            # every permanent rejection ends the document quietly
    assert 500 not in retry_codes                                     # internal errors are non-retryable by taxonomy


def test_request_bodies_conform_to_the_services_request_model(defs):
    sample = {"$.document_id": "d", "$.ingestion_id": "i", "$.parent_document_id": None, "$.trace_id": "t", "$$.Execution.Id": "arn:x"}
    seen = 0
    for d in defs.values():
        for n, s in http_states(d).items():
            body = s["Parameters"].get("RequestBody")
            if not body or "context" not in body:
                continue
            ctx = {k[:-2]: sample[v] for k, v in body["context"].items()}
            ProcessRequest.model_validate({"context": ctx, "parameters": body["parameters"]})  # extra="forbid" would reject typos
            seen += 1
    assert seen == 16   # + validation of each ZIP child, which the file workflow must run itself


def test_file_workflow_runs_stages_in_order_with_extraction_loop_and_duplicate_exit(defs):
    d = defs["file"]["States"]
    order, name = [], defs["file"]["StartAt"]
    while name in d and d[name]["Type"] == "Task" or name in ("IsDuplicate", "ExtractionDone"):
        st = d[name]
        if name.endswith("Ready"):
            name = st["Next"]
            continue
        order.append(name)
        name = st["Next"]
        if name in ("IsDuplicate", "ExtractionDone"):
            name = d[name]["Default"]
    expected = [x for st in FILE_STAGES for x in (["enrichment"] if st == "chunking" else []) + [st]]  # enrichment is a soft stage before chunking
    assert [o.lower() for o in order[:len(expected)]] == expected
    assert d["ExtractionDone"]["Choices"][0]["Next"] == "Extraction"                         # resume the next OCR batch
    assert d["IsDuplicate"]["Choices"][0]["Next"] == "ReleaseGate"
    assert d["BusinessFailure"]["Next"] == "ReleaseGate"
    for n in ("EntityExtraction", "Graph"):                                                  # optional: failures never fail the document
        assert all(c["Next"] != "MarkDocFailed" and not c["Next"].startswith("Failed") for c in d[n]["Catch"])


def test_every_required_stage_failure_marks_the_document_writes_replay_and_releases_gate(defs):
    d = defs["file"]["States"]
    for stage in FILE_STAGES:
        label = "".join(p.capitalize() for p in stage.split("-"))
        failed = d[f"Failed{label}"]
        assert failed["Result"] == stage and failed["Next"] == "MarkDocFailed"
    chain, n = [], "MarkDocFailed"
    while d[n]["Type"] != "Fail":
        chain.append(n)
        n = d[n]["Next"]
    assert chain == ["MarkDocFailed", "WriteReplayRecord", "ReleaseGateAfterFailure", "NudgeAfterFailure"]
    release = d["ReleaseGate"]["Parameters"]
    assert release["ConditionExpression"] == "holder_document_id = :d" and release["Key"]["PK"]["S"] == "GATE#GLOBAL"


def test_gate_release_matches_the_python_gate_item_layout():
    from cleandocs_shared.utils import gate_pk
    from cleandocs_shared.gate import SK

    rel = file_workflow(URLS)["States"]["ReleaseGate"]["Parameters"]
    assert rel["Key"] == {"PK": {"S": gate_pk()}, "SK": {"S": SK}}
    assert "holder_document_id" in rel["UpdateExpression"] and "lease_expires_at" in rel["UpdateExpression"]


def test_ingestion_workflow_shape(defs):
    d = defs["ingestion"]["States"]
    assert defs["ingestion"]["StartAt"] == "ValidationReady"
    assert d["IsArchive"]["Choices"][0]["Next"] == "ArchiveReady" and d["IsArchive"]["Default"] == "ManifestReady"
    assert d["PublishFiles"]["MaxConcurrency"] == 1 and d["PublishFiles"]["Next"] == "NudgeDispatcher"
    assert d["Settled"]["Choices"][0]["Next"] == "ShouldIndex"
    assert d["ShouldIndex"]["Default"] == "ReconcileAfterIndex"            # nothing to index -> still reconcile, then roll up
    assert d["ReconcileAfterIndex"]["Next"] == "ReconciliationReady" and d["ReconciliationReady"]["Next"] == "Reconcile"
    assert d["IndexRunning"]["Default"] == "ReconcileAfterIndex"
    assert d["Reconcile"]["Next"] == "ReconcileMore" and d["ReconcileMore"]["Choices"][0]["Next"] == "Reconcile"   # resumable loop
    assert d["ReconcileMore"]["Default"] == "RollUpReady"
    assert d["Reconcile"]["Catch"][-1]["Next"] == "RollUpReady" and d["ReconciliationReady"]["Catch"][0]["Next"] == "RollUpReady"
    assert d["IndexStart"]["Retry"][0]["MaxAttempts"] >= 10               # another KB job may be running
    for cap in ("SettleCapCheck", "IndexCapCheck"):                       # polling loops are capped
        assert d[cap]["Choices"][0]["Next"] == "TimedOut"
    for rejected in ("Validation", "Archive", "Manifest"):
        assert d[rejected]["Catch"][0]["ErrorEquals"] == BUSINESS and d[rejected]["Catch"][0]["Next"] == "RollUpRejected"


def test_ingestion_status_selector_matches_manifest_service_response(defs, aws_env):
    sys.path.insert(0, str(ROOT / "services" / "manifest-service"))
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from cleandocs_shared.audit import AuditWriter
    from cleandocs_shared.config import Settings
    from cleandocs_shared.registry import Registry
    from manifest_service.logic import ManifestDeps
    from manifest_service.main import create_app

    st = Settings(service_name="manifest-service", service_version="0.1.0", s3_bucket="b", service_auth_secret=SecretStr("s3cret"))
    reg = Registry(aws_env["registry_table"])
    reg.create_ingestion(ingestion_id="i", trace_id="t")
    client = TestClient(create_app(st, ManifestDeps(settings=st, registry=reg, audit=AuditWriter(aws_env["trail_table"], "manifest-service", "0.1.0"))))
    body = client.get("/api/v1/ingestions/i", headers={"Authorization": "Bearer s3cret"}).json()
    sel = defs["ingestion"]["States"]["SettlePoll"]["ResultSelector"]
    for path in sel.values():
        assert path.split(".", 2)[2] in body, path


def test_placeholders_are_all_supplied_by_the_template():
    tpl = yaml.safe_load(open(ROOT / "infrastructure" / "cleandocs-orchestration.yaml").read().replace("!", "X"))
    for name, d in (("FileStateMachine", file_workflow(URLS)), ("IngestionStateMachine", ingestion_workflow(URLS))):
        supplied = set(tpl["Resources"][name]["Properties"]["DefinitionSubstitutions"])
        assert placeholders(d) == supplied, name


def test_all_services_have_a_url_and_name_the_state_machines_consistently():
    assert set(URLS) == set(SERVICES) and len(set(URLS.values())) == len(SERVICES)
    tpl = open(ROOT / "infrastructure" / "cleandocs-orchestration.yaml").read()
    core = open(ROOT / "infrastructure" / "cleandocs-core.yaml").read()
    assert "StateMachineName: cleandocs-file" in tpl and "StateMachineName: cleandocs-ingestion" in tpl
    assert "stateMachine:cleandocs-*" in core                              # IngestUser may start the ingestion workflow


def test_dispatcher_package_uses_only_stdlib_and_boto(tmp_path):
    z = build(tmp_path)
    allowed = set(sys.stdlib_module_names) | {"boto3", "botocore", "cleandocs_shared", "handler"}
    with zipfile.ZipFile(z) as zf:
        assert set(zf.namelist()) == set(FILES)
        for n in zf.namelist():
            for node in ast.walk(ast.parse(zf.read(n))):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                       [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
                for m in mods:
                    assert m.split(".")[0] in allowed, f"{n} imports {m}"
    assert build(tmp_path).read_bytes() == z.read_bytes()                  # deterministic: same code, same hash


def test_file_workflow_validates_each_file_before_normalizing():
    """ZIP children are created by archive-service without file_kind; normalization refuses unvalidated documents with a
    business error that the workflow ends quietly, so a missing validation stage silently stalls the ingestion."""
    d = file_workflow(URLS)["States"]
    assert FILE_STAGES[0] == "validation"
    assert d[file_workflow(URLS)["StartAt"]]["Parameters"]["ApiEndpoint"].endswith("/health/ready")
    assert "/validation/" in d[file_workflow(URLS)["StartAt"]]["Parameters"]["ApiEndpoint"] or "validation" in d[file_workflow(URLS)["StartAt"]]["Parameters"]["ApiEndpoint"]
