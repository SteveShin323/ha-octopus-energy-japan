"""Async OEJP GraphQL transport using an injected aiohttp session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError

from .errors import (
    OejpInvalidResponseError,
    OejpTimeoutError,
    OejpTransportError,
    classify_graphql_errors,
)

DEFAULT_ENDPOINT = "https://api.oejp-kraken.energy/v1/graphql/"


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
        if isinstance(raw_errors, list) and raw_errors:
            errors = [error for error in raw_errors if isinstance(error, dict)]
            raise classify_graphql_errors(errors)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise OejpInvalidResponseError("GraphQL response did not contain an object data field")
        return data

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
