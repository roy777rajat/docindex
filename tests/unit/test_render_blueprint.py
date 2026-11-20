"""The Render Blueprint must stay in step with the services on disk, the IAM tiers and the workflow's service list."""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "workflows"))

from build import SERVICES as WORKFLOW_SERVICES  # noqa: E402
from build_render_yaml import SECRET_GROUPS, SERVICES, render  # noqa: E402

BP = yaml.safe_load((ROOT / "render.yaml").read_text())


def test_committed_blueprint_is_what_the_generator_produces():
    assert (ROOT / "render.yaml").read_text() == render("141571819444", "eu-west-1", "frankfurt")


def test_every_service_directory_is_deployed_and_has_a_dockerfile():
    on_disk = {p.name.removesuffix("-service") for p in (ROOT / "services").iterdir() if p.is_dir() and p.name.endswith("-service")}
    assert on_disk == set(SERVICES)
    for s in SERVICES:
        assert (ROOT / "services" / f"{s}-service" / "Dockerfile").exists()
    assert set(WORKFLOW_SERVICES) | {"upload"} == set(SERVICES)       # every service the workflows call (plus upload) is deployed


def test_blueprint_services_are_free_docker_web_services_with_one_aws_tier_each():
    assert len(BP["services"]) == 15
    for svc in BP["services"]:
        assert svc["type"] == "web" and svc["runtime"] == "docker" and svc["plan"] == "free" and svc["autoDeploy"] is False
        assert svc["healthCheckPath"] == "/health/live" and svc["dockerContext"] == "."
        groups = [e["fromGroup"] for e in svc["envVars"] if "fromGroup" in e]
        assert "cleandocs-config" in groups and "cleandocs-auth" in groups
        tiers = [g for g in groups if g.startswith("cleandocs-aws-")]
        assert len(tiers) == 1 and tiers[0] in SECRET_GROUPS


def test_iam_tiers_follow_the_phase_design():
    tier = {n: t for n, (t, _e) in SERVICES.items()}
    assert tier["upload"] == tier["archive"] == "ingest"
    assert tier["vector-indexing"] == tier["reconciliation"] == "indexing"
    assert {t for n, t in tier.items() if n not in ("upload", "archive", "vector-indexing", "reconciliation")} == {"processing"}


def test_no_secret_value_is_committed_and_secret_groups_are_not_defined_in_the_blueprint():
    text = (ROOT / "render.yaml").read_text()
    for needle in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "SERVICE_AUTH_SECRET"):
        assert needle not in text
    assert [g["name"] for g in BP["envVarGroups"]] == ["cleandocs-config"]


def test_upload_service_points_at_the_deployed_ingestion_state_machine():
    upload = next(s for s in BP["services"] if s["name"] == "cleandocs-upload-service")
    env = {e["key"]: e["value"] for e in upload["envVars"] if "key" in e}
    assert env["STEP_FUNCTIONS_INGESTION_ARN"].endswith(":stateMachine:cleandocs-ingestion")
