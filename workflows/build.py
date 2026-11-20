"""Write the generated state machine definitions.

  python workflows/build.py --url-template https://cleandocs-{service}-service.onrender.com
Output: infrastructure/generated/{ingestion,file}.asl.json (ignored by git; contains your service URLs).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from asl import file_workflow, ingestion_workflow  # noqa: E402
from validate import validate  # noqa: E402

SERVICES = ["validation", "archive", "manifest", "normalization", "cleaning", "duplicate", "extraction", "metadata",
            "chunking", "embedding", "vector-indexing", "reconciliation", "entity-extraction", "graph"]


def urls_for(template: str) -> dict[str, str]:
    return {s: template.format(service=s).rstrip("/") for s in SERVICES}


def urls_from_file(path: str) -> dict[str, str]:
    """render-urls.json written by scripts/render-urls.ps1: {"validation": "https://...onrender.com", ...}."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    missing = [s for s in SERVICES if not data.get(s)]
    if missing:
        raise SystemExit("urls file is missing: " + ", ".join(missing))
    return {s: data[s].rstrip("/") for s in SERVICES}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-template", default="https://cleandocs-{service}-service.onrender.com")
    ap.add_argument("--urls-file", help="JSON map of service -> real URL (overrides --url-template)")
    ap.add_argument("--out", default=str(Path(__file__).parents[1] / "infrastructure" / "generated"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    urls = urls_from_file(a.urls_file) if a.urls_file else urls_for(a.url_template)
    for name, defn in (("ingestion", ingestion_workflow(urls)), ("file", file_workflow(urls))):
        problems = validate(defn)
        if problems:
            raise SystemExit(f"{name}: " + "; ".join(problems))
        path = out / f"{name}.asl.json"
        path.write_text(json.dumps(defn, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path} ({len(defn['States'])} states, {path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
