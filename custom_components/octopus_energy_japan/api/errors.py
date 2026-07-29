"""Structured exceptions for the OEJP GraphQL API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphQLErrorDetail:
    """Sanitized metadata for one GraphQL operation error."""

    message: str
    error_type: str | None = None
    error_code: str | None = None
    description: str | None = None
    path: tuple[str | int, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> GraphQLErrorDetail:
        """Create an error detail from a GraphQL error object."""
        extensions = payload.get("extensions")
        if not isinstance(extensions, dict):
            extensions = {}
        raw_path = payload.get("path")
        path = tuple(raw_path) if isinstance(raw_path, list) else ()
        return cls(
            message=str(payload.get("message") or "GraphQL operation failed"),
            error_type=_optional_string(extensions.get("errorType")),
            error_code=_optional_string(extensions.get("errorCode")),
            description=_optional_string(extensions.get("errorDescription")),
            path=path,
        )


class OejpError(Exception):
    """Base exception for the OEJP client."""


class OejpTransportError(OejpError):
    """Network or HTTP transport failure."""


class OejpTimeoutError(OejpTransportError):
    """Request timeout."""


class OejpInvalidResponseError(OejpError):
    """Malformed or unsupported response."""


class OejpGraphQLError(OejpError):
    """GraphQL operation error with structured metadata."""

    def __init__(self, details: tuple[GraphQLErrorDetail, ...]) -> None:
        self.details = details
        message = "; ".join(detail.message for detail in details) or "GraphQL operation failed"
        super().__init__(message)


class OejpAuthenticationError(OejpGraphQLError):
    """Invalid or expired authentication."""


class OejpAuthorizationError(OejpGraphQLError):
    """Authenticated principal lacks permission."""


class OejpRateLimitError(OejpGraphQLError):
    """Request, point, complexity, or node limit reached."""


class OejpQueryValidationError(OejpGraphQLError):
    """GraphQL query is invalid for the active schema."""


class OejpNotFoundError(OejpGraphQLError):
    """Requested OEJP resource was not found."""


def classify_graphql_errors(errors: list[dict[str, Any]]) -> OejpGraphQLError:
    """Map OEJP GraphQL errors to a stable exception hierarchy."""
    details = tuple(GraphQLErrorDetail.from_payload(error) for error in errors)
    codes = {detail.error_code for detail in details if detail.error_code}
    types = {detail.error_type.lower() for detail in details if detail.error_type}

    if codes & {"KT-CT-1188", "KT-CT-1189", "KT-CT-1199"}:
        return OejpRateLimitError(details)
    if any("auth" in error_type for error_type in types):
        return OejpAuthenticationError(details)
    if codes & {"KT-CT-4177"} or any("permission" in error_type for error_type in types):
        return OejpAuthorizationError(details)
    if any("validation" in error_type for error_type in types):
        return OejpQueryValidationError(details)
    if any("not_found" in error_type or "notfound" in error_type for error_type in types):
        return OejpNotFoundError(details)
    return OejpGraphQLError(details)


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
