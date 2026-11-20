"""End-to-end test against the DEPLOYED system: upload one text file and wait for the reconciled result.

  $env:SERVICE_AUTH_SECRET = "<same secret as Render>"
  python scripts/smoke_test_pipeline.py --urls-file render-urls.json
  (or)  --upload-url https://... --manifest-url https://... --reconciliation-url https://...

Flow: wake the services -> POST /api/v1/uploads -> the ingestion workflow runs -> poll until the ingestion is final ->
print the reconciliation report. Exit code 0 only when the ingestion is COMPLETED. First run on free Render is slow
(each sleeping service needs about a minute to wake). Cost: a few Textract-free pages of Bedrock embedding, well under a cent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

TEXT = """CleanDocs smoke test {stamp}

Invoice INV-2026-0042 was issued to Acme Traders Pvt Ltd on 14 March 2026 for a total of Rs. 1,250.75.
Payment is due within 30 days. Questions go to billing@example.com.

Section 2: Delivery
The goods were dispatched from the Kolkata warehouse and arrived on 20 March 2026.
"""


def call(method: str, url: str, secret: str, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 120):
    req = urllib.request.Request(url, data=body, method=method, headers={"Authorization": f"Bearer {secret}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}


def wake(urls: dict[str, str], secret: str, seconds: int) -> None:
    print("waking services (free instances sleep after 15 minutes)...")
    pending, deadline = dict(urls), time.time() + seconds
    while pending and time.time() < deadline:
        for name, base in list(pending.items()):
            try:
                with urllib.request.urlopen(base + "/health/ready", timeout=30) as r:
                    if r.status == 200:
                        print(f"  ready: {name}")
                        del pending[name]
            except Exception:  # noqa: BLE001 - still starting
                pass
        if pending:
            time.sleep(10)
    if pending:
        print("  not ready in time:", ", ".join(pending))


def multipart(filename: str, data: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n").encode() \
        + data + f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"label\"\r\n\r\nsmoke-test\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls-file")
    ap.add_argument("--upload-url")
    ap.add_argument("--manifest-url")
    ap.add_argument("--reconciliation-url")
    ap.add_argument("--timeout-minutes", type=int, default=30)
    a = ap.parse_args()
    secret = os.environ.get("SERVICE_AUTH_SECRET", "")
    if not secret:
        print("set SERVICE_AUTH_SECRET first")
        return 2
    urls = json.load(open(a.urls_file, encoding="utf-8-sig")) if a.urls_file else {}
    upload = (a.upload_url or urls.get("upload", "")).rstrip("/")
    manifest = (a.manifest_url or urls.get("manifest", "")).rstrip("/")
    recon = (a.reconciliation_url or urls.get("reconciliation", "")).rstrip("/")
    if not (upload and manifest and recon):
        print("need the upload, manifest and reconciliation URLs (use --urls-file render-urls.json)")
        return 2
    wake({"upload": upload, "manifest": manifest, "reconciliation": recon}, secret, 240)

    data = TEXT.format(stamp=uuid.uuid4().hex).encode()   # unique content so the duplicate check does not skip it
    body, ctype = multipart("smoke-test.txt", data)
    code, resp = call("POST", upload + "/api/v1/uploads", secret, body, {"Content-Type": ctype, "Idempotency-Key": uuid.uuid4().hex})
    if code != 202:
        print("upload failed:", code, resp)
        return 1
    ing = resp["ingestion_id"]
    print(f"uploaded: ingestion {ing}, document {resp['document_id']}, workflow_started={resp['workflow_started']}")
    if not resp["workflow_started"]:
        print("the ingestion workflow did not start; check STEP_FUNCTIONS_INGESTION_ARN on upload-service")
        return 1

    deadline, last = time.time() + a.timeout_minutes * 60, None
    while time.time() < deadline:
        code, st = call("GET", f"{manifest}/api/v1/ingestions/{ing}", secret)
        line = (st.get("status"), json.dumps(st.get("counts")))
        if line != last:
            print(f"  [{time.strftime('%H:%M:%S')}] status={line[0]} files={line[1]}")
            last = line
        if code == 200 and st.get("status") != "PROCESSING":
            break
        time.sleep(30)
    else:
        print("timed out; check the Step Functions executions and the DLQ")
        return 1

    code, rep = call("GET", f"{recon}/api/v1/ingestions/{ing}/reconciliation", secret)
    if code != 200:
        print(f"ingestion ended {st.get('status')} but has no reconciliation report ({code}); is reconciliation-service up?")
        return 1
    print(f"\nfinal status: {rep['status']}  (finalized={rep['finalized']})")
    for d in rep["documents"]:
        bad = [f"{f['check']}: {f['detail']}" for f in d["findings"] if not f["ok"]]
        print(f"  {d['document_id']}: {d['status']} graph={d['graph_status']}" + ("" if not bad else "\n    " + "\n    ".join(bad)))
    for f in rep.get("ingestion_checks", []):
        if not f["ok"]:
            print(f"  ingestion check {f['check']}: {f['detail']}")
    return 0 if rep["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
