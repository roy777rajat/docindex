r"""Run the WHOLE pipeline on your machine against REAL AWS (S3, DynamoDB, Bedrock Knowledge Base, Textract if OCR is needed).

The 15 FastAPI apps are called in-process, in the same order the Step Functions workflows call them on Render. Nothing is
started in Step Functions and Render is not involved, so this tests the service code and the AWS permissions of YOUR
credentials (an admin profile here, not the least-privilege Render keys).

  .\.venv\Scripts\python.exe scripts\local_e2e.py                    # generated sample text file
  .\.venv\Scripts\python.exe scripts\local_e2e.py --file C:\path\report.pdf

It writes real objects to your bucket, real rows to DynamoDB, and runs one Knowledge Base ingestion job.
Cost: a fraction of a cent (Titan embeddings; Textract only for scanned pages, capped at 300 pages a month).
Exit code 0 only when the reconciled ingestion is COMPLETED.
"""
from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["upload", "validation", "archive", "manifest", "normalization", "cleaning", "duplicate", "extraction", "metadata",
            "chunking", "embedding", "entity-extraction", "graph", "vector-indexing", "reconciliation"]
SAMPLE = """CleanDocs local test {stamp}

Invoice INV-2026-0042 was issued to Acme Traders Pvt Ltd on 14 March 2026 for a total of Rs. 1,250.75.
Payment is due within 30 days. Questions go to billing@example.com.

Section 2: Delivery
The goods were dispatched from the Kolkata warehouse and arrived on 20 March 2026.
Late delivery attracts a penalty of 2.5% per week of the invoice value.
"""
AUTH = {"Authorization": "Bearer local-e2e-secret-not-for-render"}


def prepare_environment() -> None:
    """Environment must be set BEFORE the service modules are imported (they read it at import time)."""
    import boto3

    region = os.environ.setdefault("AWS_REGION", "eu-west-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    os.environ.setdefault("S3_BUCKET", f"cleandocs-{account}-{region}")
    os.environ.setdefault("BEDROCK_KNOWLEDGE_BASE_ID", "NEYHRDRGOT")
    os.environ.setdefault("BEDROCK_DATA_SOURCE_ID", "FWC2ZGVLIT")
    os.environ["SERVICE_AUTH_SECRET"] = "local-e2e-secret-not-for-render"   # in-process only
    os.environ["STEP_FUNCTIONS_INGESTION_ARN"] = ""                         # never start the real workflow from here
    for p in (ROOT / "shared", *(ROOT / "services" / f"{s}-service" for s in SERVICES)):
        sys.path.insert(0, str(p))


def load_clients() -> dict:
    from fastapi.testclient import TestClient

    clients = {}
    for s in SERVICES:
        mod = importlib.import_module(f"{s.replace('-', '_')}_service.main")
        clients[s] = TestClient(mod.app, raise_server_exceptions=False)
    return clients


class Failed(Exception):
    pass


def step(clients: dict, service: str, ctx: dict, params: dict | None = None, path: str = "/api/v1/process", label: str | None = None,
         optional: bool = False) -> dict:
    t0 = time.time()
    r = clients[service].post(path, headers=AUTH, json={"context": ctx, "parameters": params or {}}) if path.endswith("process") \
        else clients[service].post(path, headers=AUTH)
    name = label or service
    if r.status_code != 200:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:300]}
        mark = "skipped (optional)" if optional else "FAILED"
        print(f"  x {name:<18} {mark}: HTTP {r.status_code} {body.get('error_code', '')} {body.get('message', body)}")
        if optional:
            return {}
        raise Failed(name)
    out = r.json()
    status = out.get("status", out.get("state", "ok"))
    print(f"  ok {name:<18} {status:<16} {time.time() - t0:5.1f}s")
    return out.get("result", out)


def run(args: argparse.Namespace) -> int:
    prepare_environment()
    clients = load_clients()
    import boto3

    s3 = boto3.client("s3")
    bucket = os.environ["S3_BUCKET"]
    print(f"bucket {bucket}, region {os.environ['AWS_REGION']}\n")

    if args.file:
        path = Path(args.file)
        data, name, ctype = path.read_bytes(), path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    else:
        data, name, ctype = SAMPLE.format(stamp=uuid.uuid4().hex).encode(), "local-test.txt", "text/plain"

    print("1. upload")
    r = clients["upload"].post("/api/v1/uploads", headers={**AUTH, "Idempotency-Key": uuid.uuid4().hex},
                               files={"file": (name, data, ctype)}, data={"label": "local-e2e"})
    if r.status_code != 202:
        print("  upload failed:", r.status_code, r.text[:300])
        return 1
    up = r.json()
    ing, doc, trace = up["ingestion_id"], up["document_id"], up["trace_id"]
    print(f"  ok ingestion {ing}\n     document  {doc}\n     original  {up['s3_uri']}")

    base = {"ingestion_id": ing, "trace_id": trace, "workflow_execution_id": "local-e2e"}
    try:
        print("\n2. validate / expand / list files")
        val = step(clients, "validation", {**base, "document_id": doc})
        if val.get("is_archive"):
            step(clients, "archive", {**base, "document_id": doc})
        files = step(clients, "manifest", base, label="manifest").get("files", [])
        if not files:
            print("  nothing to process (rejected or empty archive)")
        for f in files:
            fctx = {**base, "document_id": f["document_id"], "parent_document_id": f.get("parent_document_id")}
            fctx = {k: v for k, v in fctx.items() if v is not None}
            print(f"\n3. file {f['filename']} ({f['document_id']})")
            step(clients, "normalization", fctx)
            step(clients, "cleaning", fctx)
            dup = step(clients, "duplicate", fctx, {"duplicate_policy": "SKIP"})
            if dup.get("skip_remaining"):
                print("  this exact file was already ingested (duplicate); stopping for this file")
                continue
            while True:
                ext = step(clients, "extraction", fctx)
                if ext.get("complete", True):
                    break
            for s in ("metadata", "chunking", "embedding"):
                step(clients, s, fctx)
            step(clients, "entity-extraction", fctx, optional=True)
            step(clients, "graph", fctx, optional=True)

        print("\n4. Knowledge Base indexing (real Bedrock ingestion job)")
        idx = step(clients, "vector-indexing", base, label="start job")
        if idx.get("state") == "RUNNING":
            deadline = time.time() + args.index_minutes * 60
            while time.time() < deadline:
                time.sleep(15)
                p = step(clients, "vector-indexing", base, path=f"/api/v1/ingestions/{ing}/index/poll", label="poll job")
                if p.get("state") != "RUNNING":
                    break
            else:
                print("  the indexing job is still running; run reconciliation later (docs/operations.md)")
                return 1

        print("\n5. reconciliation")
        while not step(clients, "reconciliation", base).get("complete", True):
            pass
    except Failed:
        print("\nstopped at the failed step; the document is marked in DynamoDB and the audit trail has the details")
        return 1

    rep = clients["reconciliation"].get(f"/api/v1/ingestions/{ing}/reconciliation", headers=AUTH).json()
    print(f"\nRESULT: {rep['status']}  counts={rep['counts']}")
    for d in rep["documents"]:
        print(f"  {d['document_id']}: {d['status']}  graph={d['graph_status']}")
        for f in d["findings"]:
            print(f"     {'ok ' if f['ok'] else 'BAD'} {f['check']:<26} {f['detail']}")
    for f in rep.get("ingestion_checks", []):
        print(f"     {'ok ' if f['ok'] else 'BAD'} {f['check']:<26} {f['detail']}")
    show_citation(s3, bucket, rep)
    print(f"\nreport: s3://{bucket}/processed/ingestions/{ing}/reconciliation.json")
    return 0 if rep["status"] == "COMPLETED" else 1


def show_citation(s3, bucket: str, rep: dict) -> None:
    """Prove the 'citation for fetched content' requirement: the sidecar the Knowledge Base returns with each hit."""
    for d in rep["documents"]:
        try:
            man = json.loads(s3.get_object(Bucket=bucket, Key=f"processed/documents/{d['document_id']}/manifests/chunks.json")["Body"].read())
            c = man["data"]["chunks"][0]
            from cleandocs_shared.utils import parse_s3_uri

            side = json.loads(s3.get_object(Bucket=bucket, Key=parse_s3_uri(c["metadata_s3_uri"])[1])["Body"].read())
            print("\ncitation metadata stored with the first chunk (returned by the Knowledge Base on a query, later):")
            print(json.dumps(side["metadataAttributes"], indent=2))
            return
        except Exception:  # noqa: BLE001 - display only
            continue


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="your own pdf/docx/xlsx/txt/image instead of the generated sample")
    ap.add_argument("--index-minutes", type=int, default=15)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
