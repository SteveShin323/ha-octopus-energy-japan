"""Async OEJP GraphQL transport using an injected aiohttp session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError

from .errors import (
    GraphQLErrorDetail,
    OejpInvalidResponseError,
    OejpTimeoutError,
    OejpTransportError,
    classify_graphql_error_details,
)

DEFAULT_ENDPOINT = "https://api.oejp-kraken.energy/v1/graphql/"


@dataclass(frozen=True, slots=True)
class GraphQLResult:
    """A validated GraphQL response that may contain partial data."""

    data: dict[str, Any] | None
    errors: tuple[GraphQLErrorDetail, ...] = ()


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
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute one GraphQL operation and return its data object."""
        result = await self.execute_optional(
            query,
            variables,
            access_token=access_token,
        )
        if result.errors:
            raise classify_graphql_error_details(result.errors)
        if result.data is None:
            raise OejpInvalidResponseError("GraphQL response did not contain an object data field")
        return result.data

    async def execute_optional(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        access_token: str | None = None,
    ) -> GraphQLResult:
        """Execute an operation while preserving valid partial data and errors."""
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"JWT {access_token}"

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session.post(
                    self._endpoint,
                    json={"query": query, "variables": dict(variables or {})},
                    headers=headers,
                ) as response:
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
        return GraphQLResult(data=data, errors=errors)

    @staticmethod
    async def _decode_response(response: ClientResponse) -> dict[str, Any]:
        if response.status >= 400:
            await response.read()
            raise OejpTransportError(f"OEJP returned HTTP {response.status}")
        try:
            payload = await response.json(content_type=None)
        except (ContentTypeError, ValueError) as err:
            raise OejpInvalidResponseError("OEJP response was not valid JSON") from err
        if not isinstance(payload, dict):
            raise OejpInvalidResponseError("OEJP response root was not an object")
        return payload
