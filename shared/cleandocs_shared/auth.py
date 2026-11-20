"""Service-to-service bearer authentication (fail closed)."""
from __future__ import annotations

import hmac
from typing import Callable

from fastapi import Header

from .exceptions import CleanDocsError, ServiceUnavailable


class Unauthorized(CleanDocsError):
    default_code = "UNAUTHORIZED"

    @property
    def http_status(self) -> int:  # noqa: D401
        return 401


def verify_bearer(authorization: str | None, expected_secret: str) -> None:
    """Raise unless `authorization` is `Bearer <expected_secret>`. Constant-time compare."""
    if not expected_secret:
        # Misconfiguration must never open the API.
        raise ServiceUnavailable("service authentication is not configured", code="AUTH_NOT_CONFIGURED")
    if not authorization or not authorization.startswith("Bearer "):
        raise Unauthorized("missing bearer token")
    presented = authorization[len("Bearer "):].encode()
    if not hmac.compare_digest(presented, expected_secret.encode()):
        raise Unauthorized("invalid bearer token")


def make_auth_dependency(get_secret: Callable[[], str]) -> Callable[..., None]:
    """Build a FastAPI dependency. `get_secret` is called per request so rotation needs only a restart."""

    def dependency(authorization: str | None = Header(default=None)) -> None:
        verify_bearer(authorization, get_secret())

    return dependency
