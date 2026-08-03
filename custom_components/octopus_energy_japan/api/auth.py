"""Authentication boundary for OEJP API operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from .client import GraphQLResult, OejpGraphQLClient
from .errors import OejpAuthenticationError, classify_graphql_error_details


class AuthSession(Protocol):
    """Provide authenticated request headers without exposing credentials."""

    async def async_get_authorization_header(self) -> str:
        """Return a current, complete Authorization header value."""

    async def async_refresh(self) -> None:
        """Refresh authentication, coalescing concurrent attempts."""

    async def async_revoke(self) -> None:
        """Revoke the current authorization when supported."""


class AuthenticatedGraphQLClient:
    """Execute GraphQL operations through an AuthSession."""

    def __init__(self, client: OejpGraphQLClient, auth: AuthSession) -> None:
        self._client = client
        self._auth = auth
        self._operation_gate = asyncio.Lock()

    async def execute(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a strict operation and retry once after authentication expiry."""
        async with self._operation_gate:
            header = await self._auth.async_get_authorization_header()
            try:
                return await self._client.execute(
                    query,
                    variables,
                    authorization_header=header,
                )
            except OejpAuthenticationError:
                await self._auth.async_refresh()
                return await self._client.execute(
                    query,
                    variables,
                    authorization_header=await self._auth.async_get_authorization_header(),
                )

    async def execute_optional(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
    ) -> GraphQLResult:
        """Execute an optional operation without treating permission errors as reauth."""
        async with self._operation_gate:
            header = await self._auth.async_get_authorization_header()
            result = await self._client.execute_optional(
                query,
                variables,
                authorization_header=header,
            )
            has_authentication_error = any(
                isinstance(
                    classify_graphql_error_details(
                        (detail,),
                        retry_after=result.retry_after,
                    ),
                    OejpAuthenticationError,
                )
                for detail in result.errors
            )
            if not has_authentication_error:
                return result
            await self._auth.async_refresh()
            return await self._client.execute_optional(
                query,
                variables,
                authorization_header=await self._auth.async_get_authorization_header(),
            )
