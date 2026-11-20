import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError

from cleandocs_shared.exceptions import (
    FailureClass, InternalFailure, UnsupportedFormat, ValidationFailure, classify_exception,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "secret document text"}}, "Op")


@pytest.mark.parametrize(
    "code,expected,retryable",
    [
        ("ThrottlingException", FailureClass.TRANSIENT_AWS, True),
        ("ProvisionedThroughputExceededException", FailureClass.TRANSIENT_AWS, True),
        ("ModelTimeoutException", FailureClass.MODEL_THROTTLED, True),
        ("ValidationException", FailureClass.VALIDATION, False),
        ("AccessDeniedException", FailureClass.INTERNAL, False),
    ],
)
def test_aws_client_errors_are_classified(code, expected, retryable):
    err = classify_exception(_client_error(code))
    assert err.failure_class is expected and err.retryable is retryable


def test_network_and_timeout_are_retryable():
    assert classify_exception(EndpointConnectionError(endpoint_url="https://x")).failure_class is FailureClass.NETWORK
    assert classify_exception(ConnectTimeoutError(endpoint_url="https://x")).failure_class is FailureClass.TIMEOUT
    assert classify_exception(TimeoutError()).retryable


def test_business_failures_are_not_retryable_and_map_to_http():
    assert not ValidationFailure("x").retryable and ValidationFailure("x").http_status == 422
    assert not UnsupportedFormat("x").retryable and UnsupportedFormat("x").http_status == 415


def test_unknown_exceptions_never_leak_their_message():
    err = classify_exception(ValueError("patient name John Doe, balance 1,234.56"))
    assert isinstance(err, InternalFailure)
    assert "John" not in err.message and "1,234" not in err.message


def test_aws_error_message_is_not_echoed():
    err = classify_exception(_client_error("ThrottlingException"))
    assert "secret document text" not in err.message
