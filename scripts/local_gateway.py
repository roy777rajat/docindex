"""One local server (default port 8001) that hosts all 16 CleanDocs services, each under its own path prefix.

  http://127.0.0.1:8001/                       index of services and their URLs
  http://127.0.0.1:8001/upload/api/v1/uploads  upload-service
  http://127.0.0.1:8001/validation/health/ready  health of validation-service (every service has /<name>/health/ready)

Real AWS with YOUR credentials. No Step Functions, no Render. Start: .\scripts\run_local.ps1   Stop: .\scripts\stop_local.ps1
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["upload", "validation", "archive", "manifest", "normalization", "cleaning", "duplicate", "extraction", "metadata", "enrichment",
            "chunking", "embedding", "entity-extraction", "graph", "vector-indexing", "reconciliation"]
SECRET = os.environ.get("SERVICE_AUTH_SECRET", "local-dev-secret-change-me-0123456789")


def prepare_environment() -> None:
    import boto3

    region = os.environ.setdefault("AWS_REGION", "eu-west-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    if not os.environ.get("S3_BUCKET"):
        account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        os.environ["S3_BUCKET"] = f"cleandocs-{account}-{region}"
    if not os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID"):   # discover from the cleandocs-core stack outputs (any AWS account)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _cfg
        c = _cfg.load(region)
        os.environ["BEDROCK_KNOWLEDGE_BASE_ID"], os.environ["BEDROCK_DATA_SOURCE_ID"] = c["kb_id"], c["ds_id"]
    os.environ["SERVICE_AUTH_SECRET"] = SECRET
    os.environ["STEP_FUNCTIONS_INGESTION_ARN"] = ""   # never start the real workflow from here
    for p in (ROOT / "shared", *(ROOT / "services" / f"{s}-service" for s in SERVICES)):
        sys.path.insert(0, str(p))


def build_app():
    from fastapi import FastAPI

    prepare_environment()
    gw = FastAPI(title="CleanDocs local gateway", docs_url=None, redoc_url=None, openapi_url=None)
    mounted = {}
    for s in SERVICES:
        mod = importlib.import_module(f"{s.replace('-', '_')}_service.main")
        gw.mount(f"/{s}", mod.app)
        mounted[s] = f"/{s}"

    @gw.get("/")
    def index():
        return {"auth_header": "Authorization: Bearer <SERVICE_AUTH_SECRET>",
                "services": {s: {"base": f"/{s}", "health": f"/{s}/health/ready"} for s in mounted}}

    return gw


app = None


def main() -> None:
    import uvicorn

    port = int(os.environ.get("LOCAL_PORT", "8001"))
    print(f"CleanDocs local gateway: http://127.0.0.1:{port}/   (index of all services)")
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
