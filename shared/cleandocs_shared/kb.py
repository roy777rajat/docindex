"""Bedrock Knowledge Base helpers shared by vector-indexing-service and reconciliation-service."""
from __future__ import annotations

from typing import Any


def kb_document_statuses(agent: Any, kb: str, ds: str) -> dict[str, str]:
    """{s3 uri: KB status} for every document the data source knows about (INDEXED, FAILED, PENDING, DELETING ...)."""
    statuses: dict[str, str] = {}
    kwargs: dict[str, Any] = {"knowledgeBaseId": kb, "dataSourceId": ds, "maxResults": 100}
    while True:
        resp = agent.list_knowledge_base_documents(**kwargs)
        for d in resp.get("documentDetails", []):
            statuses[d["identifier"]["s3"]["uri"]] = d["status"]
        if not resp.get("nextToken"):
            return statuses
        kwargs["nextToken"] = resp["nextToken"]
