import io

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cleandocs_shared.audit import AuditWriter
from cleandocs_shared.config import Settings
from cleandocs_shared.registry import Registry
from cleandocs_shared.utils import sha256_bytes
from upload_service.logic import UploadDeps, hash_stream
from upload_service.main import SERVICE_NAME, VERSION, create_app

AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def client(aws_env):
    settings = Settings(
        service_name=SERVICE_NAME, service_version=VERSION, s3_bucket=aws_env["bucket"],
        service_auth_secret=SecretStr("s3cret"), max_file_size_mb=1,
    )
    deps = UploadDeps(
        settings=settings, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
        audit=AuditWriter(aws_env["trail_table"], SERVICE_NAME, VERSION), sfn=aws_env["sfn"],
    )
    return TestClient(create_app(settings, deps), raise_server_exceptions=False), aws_env


def _post(c, data=b"%PDF-1.7 hello", name="Scan 1.pdf", ctype="application/pdf", headers=None):
    return c.post("/api/v1/uploads", files={"file": (name, io.BytesIO(data), ctype)}, headers={**AUTH, **(headers or {})})


def test_upload_stores_untouched_original_registers_and_audits(client):
    c, env = client
    data = b"%PDF-1.7 original bytes \x00\x01"
    r = _post(c, data)
    assert r.status_code == 202
    body = r.json()
    assert body["checksum_sha256"] == sha256_bytes(data) and body["size_bytes"] == len(data)
    assert body["filename"] == "Scan_1.pdf" and body["workflow_started"] is False  # no state machine configured yet

    key = body["s3_uri"].split(f"s3://{env['bucket']}/")[1]
    assert key == f"raw/documents/{body['document_id']}/original/Scan_1.pdf"
    stored = env["s3"].get_object(Bucket=env["bucket"], Key=key)
    assert stored["Body"].read() == data  # byte-identical

    reg = Registry(env["registry_table"])
    doc = reg.get_document(body["document_id"])
    assert doc["status"] == "PROCESSING" and doc["stage"] == "UPLOADED" and doc["checksum_sha256"] == body["checksum_sha256"]
    assert doc["content_type_declared"] == "application/pdf" and doc["extension"] == "pdf"
    members = reg.list_members(body["ingestion_id"])
    assert [m["document_id"] for m in members] == [body["document_id"]] and members[0]["role"] == "FILE"

    rows = env["trail_table"].scan()["Items"]
    assert {r["status"] for r in rows} == {"STARTED", "COMPLETED"}
    assert all(r["service_name"] == "upload-service" for r in rows)


def test_requires_authentication(client):
    c, _ = client
    r = c.post("/api/v1/uploads", files={"file": ("a.pdf", io.BytesIO(b"x"), "application/pdf")})
    assert r.status_code == 401


def test_size_limit_is_enforced_while_streaming(client):
    c, env = client
    r = _post(c, b"a" * (1024 * 1024 + 1))
    assert r.status_code == 422 and r.json()["error_code"] == "FILE_TOO_LARGE"
    assert env["s3"].list_objects_v2(Bucket=env["bucket"]).get("KeyCount", 0) == 0  # nothing stored


def test_empty_file_rejected(client):
    c, _ = client
    r = _post(c, b"")
    assert r.status_code == 422 and r.json()["error_code"] == "EMPTY_FILE"


def test_hostile_filename_is_neutralised(client):
    c, env = client
    r = _post(c, b"%PDF-1", name="../../etc/passwd.pdf")
    assert r.status_code == 202
    assert r.json()["s3_uri"].endswith("/original/passwd.pdf")


def test_idempotency_key_replays_without_second_upload(client):
    c, env = client
    h = {"Idempotency-Key": "abc-12345678"}
    first = _post(c, b"%PDF-1 one", headers=h).json()
    second = _post(c, b"%PDF-1 one", headers=h).json()
    assert second["document_id"] == first["document_id"] and second["idempotent_replay"] is True
    assert env["s3"].list_objects_v2(Bucket=env["bucket"])["KeyCount"] == 1
    assert _post(c, b"x", headers={"Idempotency-Key": "short"}).status_code == 422


def test_same_content_twice_gets_two_independent_documents(client):
    c, _ = client
    a, b = _post(c, b"%PDF-1 same").json(), _post(c, b"%PDF-1 same").json()
    assert a["document_id"] != b["document_id"] and a["checksum_sha256"] == b["checksum_sha256"]  # duplicate policy is later


def test_workflow_is_started_when_configured(aws_env):
    import json

    import boto3

    sfn = boto3.client("stepfunctions", region_name="eu-west-1")
    iam = boto3.client("iam", region_name="eu-west-1")
    role = iam.create_role(RoleName="r", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    arn = sfn.create_state_machine(
        name="cleandocs-ingestion", definition=json.dumps({"StartAt": "P", "States": {"P": {"Type": "Pass", "End": True}}}),
        roleArn=role,
    )["stateMachineArn"]
    settings = Settings(service_name=SERVICE_NAME, s3_bucket=aws_env["bucket"], service_auth_secret=SecretStr("s3cret"),
                        step_functions_ingestion_arn=arn)
    deps = UploadDeps(settings=settings, s3=aws_env["s3"], registry=Registry(aws_env["registry_table"]),
                      audit=AuditWriter(aws_env["trail_table"], SERVICE_NAME, VERSION), sfn=sfn)
    c = TestClient(create_app(settings, deps), raise_server_exceptions=False)
    body = _post(c, b"%PDF-1 go").json()
    assert body["workflow_started"] is True and body["workflow_execution_arn"].startswith("arn:aws:states")
    execs = sfn.list_executions(stateMachineArn=arn)["executions"]
    assert [e["name"] for e in execs] == [body["ingestion_id"]]  # execution name = ingestion id (idempotent start)


def test_health_and_execution_lookup(client):
    c, env = client
    assert c.get("/health/live").status_code == 200
    assert c.get("/health/ready").status_code == 200  # bucket and table reachable
    body = _post(c, b"%PDF-1 x").json()
    started = [r for r in env["trail_table"].scan()["Items"] if r["status"] == "STARTED"][0]
    r = c.get(f"/api/v1/executions/{started['service_execution_id']}", params={"document_id": body["document_id"]}, headers=AUTH)
    assert r.status_code == 200 and len(r.json()["events"]) == 2
    assert c.get("/api/v1/executions/nope", params={"document_id": "d"}, headers=AUTH).status_code == 422


def test_hash_stream_enforces_limit_and_rewinds():
    f = io.BytesIO(b"abc")
    digest, size = hash_stream(f, 10)
    assert size == 3 and f.read() == b"abc" and digest == sha256_bytes(b"abc")
