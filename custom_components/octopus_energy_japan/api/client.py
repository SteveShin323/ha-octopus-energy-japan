"""Async OEJP GraphQL transport using an injected aiohttp session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError

from .errors import (
    GraphQLErrorDetail,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpInvalidResponseError,
    OejpNonRetryableHttpError,
    OejpRateLimitError,
    OejpTimeoutError,
    OejpTransientHttpError,
    OejpTransportError,
    classify_graphql_error_details,
)

DEFAULT_ENDPOINT = "https://api.oejp-kraken.energy/v1/graphql/"
_TRANSIENT_HTTP_STATUSES = {408, 425, 500, 502, 503, 504}
_MAX_RETRY_AFTER = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class GraphQLResult:
    """A validated GraphQL response that may contain partial data."""

    data: dict[str, Any] | None
    errors: tuple[GraphQLErrorDetail, ...] = ()
    retry_after: timedelta | None = None


class OejpGraphQLClient:
    """Small transport client with no Home Assistant dependency."""

    def __init__(
        self,
        session: ClientSession,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        authorization_header: str | None = None,
    ) -> dict[str, Any]:
        """Execute one GraphQL operation and return its data object."""
        result = await self.execute_optional(
            query,
            variables,
            authorization_header=authorization_header,
        )
        if result.errors:
            raise classify_graphql_error_details(
                result.errors,
                retry_after=result.retry_after,
            )
        if result.data is None:  # pragma: no cover - execute_optional enforces this invariant
            raise OejpInvalidResponseError("GraphQL response did not contain an object data field")
        return result.data

    async def execute_optional(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        authorization_header: str | None = None,
    ) -> GraphQLResult:
        """Execute an operation while preserving valid partial data and errors."""
        headers = {"Accept": "application/json"}
        if authorization_header:
            headers["Authorization"] = authorization_header

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session.post(
                    self._endpoint,
                    json={"query": query, "variables": dict(variables or {})},
                    headers=headers,
                ) as response:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    payload = await self._decode_response(response)
        except TimeoutError as err:
            raise OejpTimeoutError("OEJP request timed out") from err
        except ClientError as err:
            raise OejpTransportError("OEJP network request failed") from err

        raw_errors = payload.get("errors")
        if raw_errors is None:
            errors: tuple[GraphQLErrorDetail, ...] = ()
        elif not isinstance(raw_errors, list) or any(
            not isinstance(error, dict) for error in raw_errors
        ):
            raise OejpInvalidResponseError("GraphQL errors field was malformed")
        else:
            errors = tuple(GraphQLErrorDetail.from_payload(error) for error in raw_errors)

        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            raise OejpInvalidResponseError("GraphQL response did not contain an object data field")
        if data is None and not errors:
            raise OejpInvalidResponseError("GraphQL response contained neither data nor errors")
        return GraphQLResult(data=data, errors=errors, retry_after=retry_after)

    @staticmethod
    async def _decode_response(response: ClientResponse) -> dict[str, Any]:
        if response.status >= 400:
            await response.read()
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if response.status == 401:
                raise OejpAuthenticationError((), status=response.status)
            if response.status == 403:
                raise OejpAuthorizationError((), status=response.status)
            if response.status == 429:
                raise OejpRateLimitError(
                    (),
                    retry_after=retry_after,
                    status=response.status,
                )
            if response.status in _TRANSIENT_HTTP_STATUSES:
                raise OejpTransientHttpError(
                    response.status,
                    retry_after=retry_after,
                )
            raise OejpNonRetryableHttpError(
                response.status,
                retry_after=retry_after,
            )
        try:
            payload = await response.json(content_type=None)
        except (ContentTypeError, ValueError) as err:
            raise OejpInvalidResponseError("OEJP response was not valid JSON") from err
        if not isinstance(payload, dict):
            raise OejpInvalidResponseError("OEJP response root was not an object")
        return payload


def _parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> timedelta | None:
    """Parse Retry-After seconds or HTTP-date and clamp it to one hour."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = int(stripped)
    except ValueError:
        try:
            target = parsedate_to_datetime(stripped)
        except TypeError, ValueError, OverflowError:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        delay = target.astimezone(UTC) - reference.astimezone(UTC)
    else:
        delay = timedelta(seconds=seconds)
    return min(max(delay, timedelta(0)), _MAX_RETRY_AFTER)
