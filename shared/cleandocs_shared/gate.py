"""Single-flight gate: one DynamoDB item that grants a time-limited lease to one document at a time.

Depends on boto3 only (it is packaged into the dispatcher Lambda). The same item is released by Step Functions with an
equivalent conditional UpdateItem, so every writer uses the condition `holder_document_id = :holder`.
"""
from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError

from .utils import gate_pk

SK = "LOCK"


def _conditional_failure(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


class Gate:
    def __init__(self, table: Any, scope: str = "GLOBAL") -> None:
        self._t = table
        self._key = {"PK": gate_pk(scope), "SK": SK}

    def read(self) -> dict[str, Any] | None:
        item = self._t.get_item(Key=self._key, ConsistentRead=True).get("Item")
        if not item:
            return None
        return {k: (int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, bool) else v) for k, v in item.items()}

    @staticmethod
    def holder(item: dict[str, Any] | None, now: int) -> str | None:
        """The current live holder, or None when the gate is free or the lease has expired."""
        if not item or "holder_document_id" not in item:
            return None
        return item["holder_document_id"] if int(item.get("lease_expires_at", 0)) >= now else None

    def acquire(self, document_id: str, lease_seconds: int, now: int | None = None) -> int | None:
        """Returns the fence token on success, None if another holder has a live lease."""
        now = int(now if now is not None else time.time())
        try:
            resp = self._t.update_item(
                Key=self._key,
                UpdateExpression="SET holder_document_id = :d, lease_expires_at = :exp, acquired_at = :now ADD fence :one",
                ConditionExpression="attribute_not_exists(holder_document_id) OR lease_expires_at < :now",
                ExpressionAttributeValues={":d": document_id, ":exp": now + lease_seconds, ":now": now, ":one": 1},
                ReturnValues="UPDATED_NEW",
            )
            return int(resp["Attributes"]["fence"])
        except ClientError as exc:
            if _conditional_failure(exc):
                return None
            raise

    def _guarded(self, expression: str, holder: str, values: dict[str, Any], extra_condition: str = "") -> bool:
        try:
            self._t.update_item(
                Key=self._key, UpdateExpression=expression,
                ConditionExpression="holder_document_id = :holder" + extra_condition,
                ExpressionAttributeValues={":holder": holder, **values},
            )
            return True
        except ClientError as exc:
            if _conditional_failure(exc):
                return False
            raise

    def set_workflow(self, document_id: str, workflow_arn: str) -> bool:
        return self._guarded("SET workflow_arn = :w", document_id, {":w": workflow_arn})

    def extend(self, document_id: str, lease_seconds: int, now: int | None = None) -> bool:
        now = int(now if now is not None else time.time())
        return self._guarded("SET lease_expires_at = :exp", document_id, {":exp": now + lease_seconds})

    def release(self, document_id: str) -> bool:
        """Only the holder can release. False means the lease was already released or taken over."""
        return self._guarded("REMOVE holder_document_id, workflow_arn, lease_expires_at", document_id, {})
