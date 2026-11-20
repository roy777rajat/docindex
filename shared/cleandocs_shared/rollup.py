"""Final-status rule shared by manifest-service (live status) and reconciliation-service (evidence-based status)."""
from __future__ import annotations

from typing import Any

TERMINAL = {"COMPLETED", "DUPLICATE", "REJECTED", "FAILED", "PARTIAL"}
SUCCESS = {"COMPLETED", "DUPLICATE"}


def rollup(members: list[dict[str, Any]]) -> dict[str, Any]:
    files = [m for m in members if m.get("role", "FILE") == "FILE"]
    counts: dict[str, int] = {}
    for m in files:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    all_terminal = bool(files) and all(m["status"] in TERMINAL for m in files)
    if not files or not all_terminal:
        status = "PROCESSING"
    else:
        ok = sum(1 for m in files if m["status"] in SUCCESS)
        usable = ok + counts.get("PARTIAL", 0)  # a PARTIAL document still holds usable content
        status = "COMPLETED" if ok == len(files) else "FAILED" if usable == 0 else "PARTIAL"
    return {"status": status, "all_terminal": all_terminal, "total_files": len(files), "counts": counts,
            "archives": [m["document_id"] for m in members if m.get("role") == "ARCHIVE"]}
