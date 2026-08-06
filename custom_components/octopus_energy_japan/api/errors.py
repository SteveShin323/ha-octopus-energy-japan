"""Structured exceptions for the OEJP GraphQL API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

_SAFE_ERROR_MESSAGE = "GraphQL operation failed"
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUTHENTICATION_TYPES = {"AUTHENTICATION", "UNAUTHENTICATED"}
_AUTHORIZATION_TYPES = {"AUTHORIZATION", "FORBIDDEN", "PERMISSION"}
# OEJP reports a rejected credential as errorType VALIDATION rather than
# AUTHENTICATION, so the code table has to win over the type. Observed on
# 2026-08-04: obtainKrakenToken with a wrong password returned
# VALIDATION/KT-CT-1138, and the same document with the correct password
# returned a token.
#
# KT-CT-1124 is "Signature of the JWT has expired", and it arrives as errorType
# APPLICATION — the type says nothing about authentication and the code is the only
# signal. Measured on 2026-08-06 by replaying a stored access token that had gone stale.
# Until it was listed here the expiry was an ordinary operation failure, so the one thing
# that fixes it never ran: `AuthenticatedGraphQLClient.execute` refreshes and retries only
# for `OejpAuthenticationError`. A stored token is used without checking its age, so an
# installation that restarted more than a token lifetime after its last refresh could not
# set up at all, and retried forever with the same dead token.
_AUTHENTICATION_CODES = {"KT-CT-1120", "KT-CT-1124", "KT-CT-1138"}
_AUTHORIZATION_CODES = {"KT-CT-1112", "KT-CT-4177"}
_RATE_LIMIT_CODES = {"KT-CT-1188", "KT-CT-1189", "KT-CT-1199"}


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
        return cls(
            # Provider-rendered messages and descriptions can contain customer
            # identifiers. Keep only allow-listed structured metadata.
            message=_SAFE_ERROR_MESSAGE,
            error_type=_optional_error_identifier(extensions.get("errorType")),
            error_code=_optional_error_identifier(extensions.get("errorCode")),
            description=None,
            path=_sanitize_path(payload.get("path")),
        )


class OejpError(Exception):
    """Base exception for the OEJP client."""


class OejpTransportError(OejpError):
    """Network or HTTP transport failure."""


class OejpHttpError(OejpTransportError):
    """Typed non-success HTTP response without provider-rendered content."""

    def __init__(
        self,
        status: int,
        *,
        retry_after: timedelta | None = None,
    ) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"OEJP returned HTTP {status}")


class OejpTransientHttpError(OejpHttpError):
    """HTTP response that background synchronization may retry."""


class OejpNonRetryableHttpError(OejpHttpError):
    """HTTP response that must not be retried automatically."""


class OejpTimeoutError(OejpTransportError):
    """Request timeout."""


class OejpInvalidResponseError(OejpError):
    """Malformed or unsupported response."""


class OejpGraphQLError(OejpError):
    """GraphQL operation error with structured metadata."""

    def __init__(
        self,
        details: tuple[GraphQLErrorDetail, ...],
        *,
        retry_after: timedelta | None = None,
        status: int | None = None,
    ) -> None:
        self.details = details
        self.retry_after = retry_after
        self.status = status
        markers = sorted(
            {
                "/".join(
                    value for value in (detail.error_type, detail.error_code) if value is not None
                )
                for detail in details
            }
            - {""}
        )
        suffix = f" ({', '.join(markers)})" if markers else ""
        message = f"OEJP {_SAFE_ERROR_MESSAGE.lower()}{suffix}"
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
    return classify_graphql_error_details(details)


def classify_graphql_error_details(
    details: tuple[GraphQLErrorDetail, ...],
    *,
    retry_after: timedelta | None = None,
) -> OejpGraphQLError:
    """Map sanitized GraphQL error details to a stable exception hierarchy."""
    codes = {detail.error_code for detail in details if detail.error_code}
    types = {_normalize_error_type(detail.error_type) for detail in details if detail.error_type}

    if codes & _RATE_LIMIT_CODES:
        return OejpRateLimitError(details, retry_after=retry_after)
    if codes & _AUTHENTICATION_CODES:
        return OejpAuthenticationError(details)
    if codes & _AUTHORIZATION_CODES or types & _AUTHORIZATION_TYPES:
        return OejpAuthorizationError(details)
    if types & _AUTHENTICATION_TYPES:
        return OejpAuthenticationError(details)
    if "VALIDATION" in types:
        return OejpQueryValidationError(details)
    if types & {"NOT_FOUND", "NOTFOUND"}:
        return OejpNotFoundError(details)
    return OejpGraphQLError(details)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_error_identifier(value: object) -> str | None:
    """Return only bounded identifiers that are safe to expose in diagnostics."""
    identifier = _optional_string(value)
    if identifier is None or _SAFE_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        return None
    return identifier


def _sanitize_path(value: object) -> tuple[str | int, ...]:
    """Drop GraphQL path components that could contain arbitrary provider text."""
    if not isinstance(value, list):
        return ()

    path: list[str | int] = []
    for component in value:
        if isinstance(component, int):
            path.append(component)
        elif identifier := _optional_error_identifier(component):
            path.append(identifier)
    return tuple(path)


def _normalize_error_type(value: str) -> str:
    return value.strip().replace("-", "_").upper()
