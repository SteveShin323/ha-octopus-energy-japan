"""Tests for the authenticated GraphQL boundary."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    AuthenticatedGraphQLClient,
    GraphQLErrorDetail,
    GraphQLResult,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpGraphQLClient,
)


def _graphql_error(
    error_class: type[OejpAuthenticationError] | type[OejpAuthorizationError],
) -> Exception:
    return error_class(
        (
            GraphQLErrorDetail(
                message="GraphQL operation failed",
                error_type=(
                    "AUTHENTICATION" if error_class is OejpAuthenticationError else "AUTHORIZATION"
                ),
            ),
        )
    )


async def test_strict_operation_refreshes_once_after_authentication_error() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.side_effect = [
        _graphql_error(OejpAuthenticationError),
        {"viewer": {"id": "viewer"}},
    ]
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = ["Bearer old", "Bearer new"]

    result = await AuthenticatedGraphQLClient(client, auth).execute("query Viewer { viewer }")

    assert result == {"viewer": {"id": "viewer"}}
    auth.async_refresh.assert_awaited_once_with()
    assert [call.kwargs["authorization_header"] for call in client.execute.await_args_list] == [
        "Bearer old",
        "Bearer new",
    ]


async def test_strict_operation_does_not_refresh_authorization_error() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.side_effect = _graphql_error(OejpAuthorizationError)
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"

    with pytest.raises(OejpAuthorizationError):
        await AuthenticatedGraphQLClient(client, auth).execute("query Viewer { viewer }")

    auth.async_refresh.assert_not_awaited()


async def test_optional_operation_preserves_permission_partial_response() -> None:
    partial = GraphQLResult(
        data={"viewer": {"accounts": []}},
        errors=(
            GraphQLErrorDetail(
                message="GraphQL operation failed",
                error_type="AUTHORIZATION",
            ),
        ),
    )
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute_optional.return_value = partial
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"

    result = await AuthenticatedGraphQLClient(client, auth).execute_optional(
        "query Viewer { viewer }"
    )

    assert result is partial
    auth.async_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "detail",
    [
        GraphQLErrorDetail(
            message="GraphQL operation failed",
            error_type="AUTHENTICATION",
        ),
        GraphQLErrorDetail(
            message="GraphQL operation failed",
            error_code="KT-CT-1120",
        ),
    ],
)
async def test_optional_operation_retries_authentication_partial_response(
    detail: GraphQLErrorDetail,
) -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(data={"viewer": None}, errors=(detail,)),
        GraphQLResult(data={"viewer": {"id": "viewer"}}),
    ]
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = ["Bearer old", "Bearer new"]

    result = await AuthenticatedGraphQLClient(client, auth).execute_optional(
        "query Viewer { viewer }"
    )

    assert result.data == {"viewer": {"id": "viewer"}}
    auth.async_refresh.assert_awaited_once_with()


async def test_optional_operation_refreshes_when_auth_and_permission_errors_are_mixed() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(
            data={"viewer": None},
            errors=(
                GraphQLErrorDetail("safe", error_type="AUTHORIZATION"),
                GraphQLErrorDetail("safe", error_type="AUTHENTICATION"),
            ),
        ),
        GraphQLResult(data={"viewer": {"id": "viewer"}}),
    ]
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = ["Bearer old", "Bearer new"]

    result = await AuthenticatedGraphQLClient(client, auth).execute_optional(
        "query Viewer { viewer }"
    )

    assert result.data == {"viewer": {"id": "viewer"}}
    auth.async_refresh.assert_awaited_once_with()


async def test_operation_gate_spans_header_refresh_and_retry() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
    active = 0
    maximum_active = 0
    calls = 0

    async def execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal active, calls, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        if calls == 1:
            raise _graphql_error(OejpAuthenticationError)
        return {"viewer": {"id": "viewer"}}

    async def refresh() -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1

    client.execute.side_effect = execute
    auth.async_refresh.side_effect = refresh
    gated = AuthenticatedGraphQLClient(client, auth)

    results = await asyncio.gather(
        gated.execute("query First { viewer { id } }"),
        gated.execute("query Second { viewer { id } }"),
    )

    assert results == [
        {"viewer": {"id": "viewer"}},
        {"viewer": {"id": "viewer"}},
    ]
    assert calls == 3
    assert maximum_active == 1
