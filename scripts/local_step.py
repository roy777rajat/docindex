r"""Call the LOCAL services one step at a time (start them first with .\scripts\run_local.ps1).

  python scripts\local_step.py upload C:\docs\report.pdf     upload; remembers the ids in .local-state.json
  python scripts\local_step.py validate                      validation-service   (add: archive  if it says is_archive)
  python scripts\local_step.py manifest                      manifest-service: which files to process
  python scripts\local_step.py run normalization             one stage for every file (then: cleaning, duplicate, extraction,
  python scripts\local_step.py run cleaning                  metadata, chunking, embedding, entity-extraction, graph)
  python scripts\local_step.py index                         start the Knowledge Base job and wait for it
  python scripts\local_step.py reconcile                     evidence-based final status + report
  python scripts\local_step.py status | show S3KEY | reset   status / print an S3 JSON artifact / forget the ingestion

Every command prints the service's JSON answer (trimmed). Open http://127.0.0.1:8001/<service>/docs for each service's own page.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE.parent / ".local-state.json"
BASE = os.environ.get("LOCAL_BASE", "http://127.0.0.1:8001")
SECRET = os.environ.get("SERVICE_AUTH_SECRET", "local-dev-secret-change-me-0123456789")
FILE_STAGES = ["normalization", "cleaning", "duplicate", "extraction", "metadata", "enrichment", "chunking", "embedding", "entity-extraction", "graph"]
OPTIONAL = {"entity-extraction", "graph"}


def http(method: str, svc: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout: int = 300):
    req = urllib.request.Request(f"{BASE}/{svc}{path}", data=body, method=method,
                                 headers={"Authorization": f"Bearer {SECRET}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:400]}
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {BASE} ({e.reason}). Start it with .\\scripts\\run_local.ps1")


def post(svc: str, ctx: dict, params: dict | None = None, path: str = "/api/v1/process"):
    return http("POST", svc, path, json.dumps({"context": ctx, "parameters": params or {}}).encode(), {"Content-Type": "application/json"})


def load() -> dict:
    if not STATE.exists():
        raise SystemExit("no ingestion yet: python scripts\\local_step.py upload <file>")
    return json.loads(STATE.read_text())


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def ctx_of(st: dict, doc: dict | None = None) -> dict:
    c = {"ingestion_id": st["ingestion_id"], "trace_id": st["trace_id"], "workflow_execution_id": "local-step"}
    if doc:
        c["document_id"] = doc["document_id"]
        if doc.get("parent_document_id"):
            c["parent_document_id"] = doc["parent_document_id"]
    return c


def show(code: int, body: dict, label: str) -> bool:
    ok = code == 200
    print(f"{'ok' if ok else 'FAILED'}  {label}  HTTP {code}")
    print(json.dumps(body.get("result", body) if ok else body, indent=2)[:3500])
    return ok


def multipart(name: str, data: bytes, owner: str = "") -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    ctype = {"pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv", "png": "image/png", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "zip": "application/zip"
             }.get(name.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n').encode() \
        + data + f'\r\n--{b}\r\nContent-Disposition: form-data; name="label"\r\n\r\nlocal-step\r\n'.encode() \
        + (f'--{b}\r\nContent-Disposition: form-data; name="owner"\r\n\r\n{owner}\r\n'.encode() if owner else b"") \
        + f'--{b}--\r\n'.encode()
    return body, f"multipart/form-data; boundary={b}"


def cmd_upload(a) -> int:
    p = Path(a.path)
    body, ctype = multipart(p.name, p.read_bytes(), a.owner)
    code, out = http("POST", "upload", "/api/v1/uploads", body, {"Content-Type": ctype, "Idempotency-Key": uuid.uuid4().hex})
    if code != 202:
        return 0 if show(code, out, "upload") else 1
    root = {"document_id": out["document_id"], "filename": out["filename"]}
    save({"ingestion_id": out["ingestion_id"], "trace_id": out["trace_id"], "root": root, "documents": [root]})
    print(f"ok  upload\n  ingestion {out['ingestion_id']}\n  document  {out['document_id']}\n  original  {out['s3_uri']}")
    return 0


def cmd_validate(a) -> int:
    st = load()
    code, out = post("validation", ctx_of(st, st["root"]))
    ok = show(code, out, "validation")
    if ok and out["result"].get("is_archive"):
        print("\nthis is a ZIP: next run  archive  then  manifest")
    return 0 if ok else 1


def cmd_archive(a) -> int:
    st = load()
    code, out = post("archive", ctx_of(st, st["root"]))
    return 0 if show(code, out, "archive") else 1


def cmd_manifest(a) -> int:
    st = load()
    code, out = post("manifest", ctx_of(st))
    if not show(code, out, "manifest"):
        return 1
    st["documents"] = [{"document_id": f["document_id"], "parent_document_id": f.get("parent_document_id"), "filename": f["filename"]}
                       for f in out["result"]["files"]]
    save(st)
    print(f"\n{len(st['documents'])} file(s) to process")
    return 0


def cmd_run(a) -> int:
    st = load()
    docs = [d for d in st["documents"] if not d.get("skipped") and (not a.document or d["document_id"] == a.document)]
    if not docs:
        print("no files to run (run manifest first, or all were skipped as duplicates)")
        return 1
    rc = 0
    for d in docs:
        if a.service == "normalization" and d.get("parent_document_id"):   # ZIP children are validated here, as the file workflow does
            vcode, vout = post("validation", ctx_of(st, d))
            if not show(vcode, vout, f"validation (ZIP child {d.get('filename')})"):
                rc = 1
                continue
        print(f"\n== {a.service} :: {d.get('filename')} ({d['document_id']})")
        while True:
            code, out = post(a.service, ctx_of(st, d), {"duplicate_policy": "SKIP"} if a.service == "duplicate" else None)
            if not show(code, out, a.service):
                rc = 0 if a.service in OPTIONAL else 1
                break
            res = out.get("result", {})
            if a.service == "extraction" and res.get("complete") is False:
                print("  (more OCR pages remain; calling again)")
                continue
            if a.service == "duplicate" and res.get("skip_remaining"):
                d["skipped"] = True
                print("  this exact file was ingested before: it is a DUPLICATE, later stages are skipped")
            break
    save(st)
    nxt = FILE_STAGES[FILE_STAGES.index(a.service) + 1] if a.service in FILE_STAGES[:-1] else "index"
    print(f"\nnext: python scripts\\local_step.py {'run ' + nxt if nxt != 'index' else 'index'}")
    return rc


def cmd_index(a) -> int:
    st = load()
    code, out = post("vector-indexing", ctx_of(st))
    if not show(code, out, "start indexing job"):
        return 1
    if out["result"].get("state") != "RUNNING":
        return 0
    deadline = time.time() + a.minutes * 60
    while time.time() < deadline:
        time.sleep(15)
        code, out = http("POST", "vector-indexing", f"/api/v1/ingestions/{st['ingestion_id']}/index/poll")
        print(f"  [{time.strftime('%H:%M:%S')}] {out.get('state', out)}")
        if code != 200:
            show(code, out, "poll")
            return 1
        if out.get("state") != "RUNNING":
            show(code, out, "indexing finished")
            return 0 if out.get("state") == "COMPLETE" and not out.get("documents_failed") else 1
    print("still running; run `index` again later to keep waiting")
    return 1


def print_report(rep: dict) -> None:
    print(f"\nRESULT: {rep['status']}  counts={rep['counts']}")
    for d in rep["documents"]:
        print(f"  {d['document_id']}: {d['status']}  graph={d['graph_status']}")
        for f in d["findings"]:
            print(f"     {'ok ' if f['ok'] else 'BAD'} {f['check']:<26} {f['detail']}")
    for f in rep.get("ingestion_checks", []):
        print(f"     {'ok ' if f['ok'] else 'BAD'} {f['check']:<26} {f['detail']}")


def cmd_reconcile(a) -> int:
    st = load()
    while True:
        code, out = post("reconciliation", ctx_of(st), {"force": True} if a.force else None)
        if not show(code, out, "reconciliation"):
            return 1
        if out["result"].get("complete", True):
            break
    code, rep = http("GET", "reconciliation", f"/api/v1/ingestions/{st['ingestion_id']}/reconciliation")
    if code == 200:
        print_report(rep)
    return 0 if code == 200 and rep["status"] == "COMPLETED" else 1


def cmd_status(a) -> int:
    st = load()
    code, out = http("GET", "manifest", f"/api/v1/ingestions/{st['ingestion_id']}")
    show(code, out, "ingestion status (manifest-service)")
    code, rep = http("GET", "reconciliation", f"/api/v1/ingestions/{st['ingestion_id']}/reconciliation")
    if code == 200:
        print_report(rep)
    return 0


def cmd_show(a) -> int:
    import boto3

    bucket = os.environ.get("S3_BUCKET") or f"cleandocs-{boto3.client('sts').get_caller_identity()['Account']}-{os.environ.get('AWS_REGION', 'eu-west-1')}"
    st = load()
    key = a.key.replace("{doc}", st["root"]["document_id"])
    print(f"s3://{bucket}/{key}")
    print(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode()[:a.max_chars])
    return 0


def cmd_reset(a) -> int:
    STATE.unlink(missing_ok=True)
    print("forgot the current ingestion (data stays in AWS)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("upload"); s.add_argument("path"); s.add_argument("--owner", default=""); s.set_defaults(fn=cmd_upload)
    for n, fn in (("validate", cmd_validate), ("archive", cmd_archive), ("manifest", cmd_manifest), ("status", cmd_status), ("reset", cmd_reset)):
        sub.add_parser(n).set_defaults(fn=fn)
    s = sub.add_parser("run"); s.add_argument("service", choices=FILE_STAGES); s.add_argument("--document"); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("index"); s.add_argument("--minutes", type=int, default=15); s.set_defaults(fn=cmd_index)
    s = sub.add_parser("reconcile"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_reconcile)
    s = sub.add_parser("show"); s.add_argument("key", help="S3 key; {doc} is replaced by the uploaded document id")
    s.add_argument("--max-chars", type=int, default=4000); s.set_defaults(fn=cmd_show)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
