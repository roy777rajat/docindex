"""Repository for the `cleandocs-registry` table (mutable document / ingestion state).

Key layout (see docs/phase0-architecture.md section 6.3):
  DOC#{id}      / META          document state
  INGEST#{id}   / META          ingestion state
  INGEST#{id}   / DOC#{id}      manifest membership (one per file or archive in the ingestion)
  IDEMP#{key}   / CLAIM         stored response for an Idempotency-Key
"""
from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .models import DocumentStage, DocumentStatus, GraphStatus
from .utils import from_dynamo, iso, registry_doc_pk, registry_ingest_pk, to_dynamo

META = "META"


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


class Registry:
    def __init__(self, table: Any) -> None:
        self._t = table

    # ---- generic helpers ----
    def _update(self, pk: str, sk: str, fields: dict[str, Any], condition: str | None = None) -> None:
        fields = {**fields, "updated_at": iso()}
        names, values, sets = {}, {}, []
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"] = k
            values[f":v{i}"] = to_dynamo(v)
            sets.append(f"#f{i} = :v{i}")
        kwargs: dict[str, Any] = {
            "Key": {"PK": pk, "SK": sk},
            "UpdateExpression": "SET " + ", ".join(sets),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            # never create a phantom item from an update
            "ConditionExpression": "attribute_exists(PK)" + (f" AND ({condition})" if condition else ""),
        }
        self._t.update_item(**kwargs)

    def _put_new(self, item: dict[str, Any]) -> bool:
        try:
            self._t.put_item(
                Item={k: to_dynamo(v) for k, v in item.items() if v is not None},
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    # ---- documents ----
    def create_document(
        self, *, document_id: str, ingestion_id: str, original_filename: str, extension: str,
        size_bytes: int, checksum_sha256: str, s3_uri_original: str,
        content_type_declared: str | None = None, parent_document_id: str | None = None,
        archive_entry_path: str | None = None, archive_depth: int = 0,
        owner: str = "ADMIN",
    ) -> bool:
        """Returns False if the document already exists (idempotent create)."""
        now = iso()
        return self._put_new({
            "PK": registry_doc_pk(document_id), "SK": META,
            "document_id": document_id, "ingestion_id": ingestion_id,
            "parent_document_id": parent_document_id, "owner": owner,
            "original_filename": original_filename, "extension": extension,
            "archive_entry_path": archive_entry_path, "archive_depth": archive_depth,
            "size_bytes": size_bytes, "checksum_sha256": checksum_sha256,
            "content_type_declared": content_type_declared, "s3_uri_original": s3_uri_original,
            "stage": DocumentStage.UPLOADED.value, "status": DocumentStatus.PROCESSING.value,
            "graph_status": GraphStatus.NOT_CONFIGURED.value, "indexing_state": "PENDING",
            "uploaded_at": now, "created_at": now, "updated_at": now,
        })

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        item = self._t.get_item(Key={"PK": registry_doc_pk(document_id), "SK": META}).get("Item")
        return from_dynamo(item) if item else None

    def update_document(self, document_id: str, **fields: Any) -> None:
        self._update(registry_doc_pk(document_id), META, fields)

    # ---- ingestions and manifest membership ----
    def create_ingestion(self, *, ingestion_id: str, trace_id: str, label: str | None = None, owner: str = "ADMIN") -> bool:
        now = iso()
        return self._put_new({
            "PK": registry_ingest_pk(ingestion_id), "SK": META, "ingestion_id": ingestion_id,
            "trace_id": trace_id, "label": label, "owner": owner, "status": DocumentStatus.PROCESSING.value,
            "created_at": now, "updated_at": now,
        })

    def get_ingestion(self, ingestion_id: str) -> dict[str, Any] | None:
        item = self._t.get_item(Key={"PK": registry_ingest_pk(ingestion_id), "SK": META}).get("Item")
        return from_dynamo(item) if item else None

    def update_ingestion(self, ingestion_id: str, **fields: Any) -> None:
        self._update(registry_ingest_pk(ingestion_id), META, fields)

    def add_member(
        self, *, ingestion_id: str, document_id: str, role: str = "FILE",
        status: str = DocumentStatus.PROCESSING.value, **attrs: Any,
    ) -> bool:
        """role: FILE (processed individually) or ARCHIVE (container, not processed as content)."""
        now = iso()
        return self._put_new({
            "PK": registry_ingest_pk(ingestion_id), "SK": f"DOC#{document_id}",
            "ingestion_id": ingestion_id, "document_id": document_id, "role": role, "status": status,
            "created_at": now, "updated_at": now, **attrs,
        })

    def update_member(self, ingestion_id: str, document_id: str, **fields: Any) -> None:
        self._update(registry_ingest_pk(ingestion_id), f"DOC#{document_id}", fields)

    def list_members(self, ingestion_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(registry_ingest_pk(ingestion_id)) & Key("SK").begins_with("DOC#")
        }
        while True:
            resp = self._t.query(**kwargs)
            items.extend(from_dynamo(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # ---- idempotency keys ----
    def get_idempotent_response(self, key: str) -> dict[str, Any] | None:
        item = self._t.get_item(Key={"PK": f"IDEMP#{key}", "SK": "CLAIM"}).get("Item")
        return from_dynamo(item["response"]) if item else None

    def store_idempotent_response(self, key: str, response: dict[str, Any]) -> bool:
        return self._put_new({"PK": f"IDEMP#{key}", "SK": "CLAIM", "response": response, "created_at": iso()})

    # ---- exact-duplicate claims and monthly cost counters ----
    def claim_checksum(self, checksum: str, document_id: str) -> str:
        """First document to claim a checksum becomes canonical. Returns the canonical document id (idempotent)."""
        from .utils import checksum_pk

        key = {"PK": checksum_pk(checksum), "SK": "CLAIM"}
        if self._put_new({**key, "canonical_document_id": document_id, "claimed_at": iso()}):
            return document_id
        item = self._t.get_item(Key=key, ConsistentRead=True)["Item"]
        return item["canonical_document_id"]

    def reserve_counter(self, year_month: str, name: str, amount: int, cap: int) -> bool:
        """Atomically add to a monthly counter unless it would exceed the cap. Returns False when over budget."""
        from .utils import budget_pk

        if amount > cap:
            return False
        try:
            self._t.update_item(
                Key={"PK": budget_pk(year_month), "SK": "COUNTERS"},
                UpdateExpression="ADD #n :a",
                ConditionExpression="attribute_not_exists(#n) OR #n <= :room",
                ExpressionAttributeNames={"#n": name},
                ExpressionAttributeValues={":a": amount, ":room": cap - amount},
            )
            return True
        except ClientError as exc:
            if _is_conditional_failure(exc):
                return False
            raise
