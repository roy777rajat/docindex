"""Failure taxonomy. Retryability is a property of the failure class, not of call sites."""
from __future__ import annotations

from enum import Enum


class FailureClass(str, Enum):
    VALIDATION = "VALIDATION"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    PERMANENT_BUSINESS = "PERMANENT_BUSINESS"
    TRANSIENT_AWS = "TRANSIENT_AWS"
    NETWORK = "NETWORK"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MODEL_THROTTLED = "MODEL_THROTTLED"
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"


RETRYABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.TRANSIENT_AWS,
        FailureClass.NETWORK,
        FailureClass.SERVICE_UNAVAILABLE,
        FailureClass.MODEL_THROTTLED,
        FailureClass.TIMEOUT,
    }
)

HTTP_STATUS: dict[FailureClass, int] = {
    FailureClass.VALIDATION: 422,
    FailureClass.UNSUPPORTED_FORMAT: 415,
    FailureClass.PERMANENT_BUSINESS: 409,
    FailureClass.TRANSIENT_AWS: 503,
    FailureClass.NETWORK: 503,
    FailureClass.SERVICE_UNAVAILABLE: 503,
    FailureClass.MODEL_THROTTLED: 429,
    FailureClass.TIMEOUT: 504,
    FailureClass.INTERNAL: 500,
}


class CleanDocsError(Exception):
    failure_class: FailureClass = FailureClass.INTERNAL
    default_code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.failure_class in RETRYABLE

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.failure_class]


class ValidationFailure(CleanDocsError):
    failure_class = FailureClass.VALIDATION
    default_code = "VALIDATION_FAILED"


class UnsupportedFormat(CleanDocsError):
    failure_class = FailureClass.UNSUPPORTED_FORMAT
    default_code = "UNSUPPORTED_FORMAT"


class PermanentBusinessFailure(CleanDocsError):
    failure_class = FailureClass.PERMANENT_BUSINESS
    default_code = "BUSINESS_RULE_VIOLATION"


class NotFound(PermanentBusinessFailure):
    default_code = "NOT_FOUND"

    @property
    def http_status(self) -> int:
        return 404


class TransientAwsFailure(CleanDocsError):
    failure_class = FailureClass.TRANSIENT_AWS
    default_code = "AWS_TRANSIENT"


class NetworkFailure(CleanDocsError):
    failure_class = FailureClass.NETWORK
    default_code = "NETWORK_ERROR"


class ServiceUnavailable(CleanDocsError):
    failure_class = FailureClass.SERVICE_UNAVAILABLE
    default_code = "SERVICE_UNAVAILABLE"


class ModelThrottled(CleanDocsError):
    failure_class = FailureClass.MODEL_THROTTLED
    default_code = "MODEL_THROTTLED"


class StageTimeout(CleanDocsError):
    failure_class = FailureClass.TIMEOUT
    default_code = "STAGE_TIMEOUT"


class InternalFailure(CleanDocsError):
    failure_class = FailureClass.INTERNAL
    default_code = "INTERNAL_ERROR"


_TRANSIENT_AWS_CODES = frozenset(
    {
        "ThrottlingException", "Throttling", "ThrottledException", "RequestLimitExceeded",
        "ProvisionedThroughputExceededException", "RequestThrottled", "SlowDown",
        "ServiceUnavailable", "InternalError", "InternalServerError", "InternalFailure",
        "RequestTimeout", "RequestTimeoutException", "TransactionInProgressException",
        "LimitExceededException",
    }
)
_MODEL_THROTTLE_CODES = frozenset({"ModelTimeoutException", "ModelNotReadyException", "TooManyRequestsException"})


def classify_exception(exc: BaseException) -> CleanDocsError:
    """Map any exception to a CleanDocsError so callers apply one retry policy."""
    if isinstance(exc, CleanDocsError):
        return exc
    try:
        from botocore.exceptions import (
            ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError,
        )
    except ImportError:  # pragma: no cover
        return InternalFailure(type(exc).__name__)

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _MODEL_THROTTLE_CODES:
            return ModelThrottled(f"{code}", code=code)
        if code in _TRANSIENT_AWS_CODES:
            return TransientAwsFailure(f"{code}", code=code)
        if code in {"ValidationException", "InvalidParameterValue", "InvalidArgument"}:
            return ValidationFailure(f"{code}", code=code)
        return InternalFailure(f"AWS error {code or 'unknown'}", code=code or None)
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError)):
        return StageTimeout(type(exc).__name__)
    if isinstance(exc, (EndpointConnectionError, ConnectionClosedError, ConnectionError)):
        return NetworkFailure(type(exc).__name__)
    if isinstance(exc, TimeoutError):
        return StageTimeout("TimeoutError")
    # Deliberately do not echo str(exc): it may contain document content.
    return InternalFailure(type(exc).__name__)
