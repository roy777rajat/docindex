"""Write JSON Schemas for the graph contracts to docs/schemas/. Run: python scripts/export_schemas.py"""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "shared"))
from cleandocs_shared.graph_models import Entity, EvidenceRef, Relationship  # noqa: E402

out = root / "docs" / "schemas"
out.mkdir(parents=True, exist_ok=True)
for model in (EvidenceRef, Entity, Relationship):
    path = out / f"{model.__name__.lower()}.schema.json"
    path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    print("wrote", path)
