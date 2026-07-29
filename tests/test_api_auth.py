"""Tests for the authenticated GraphQL boundary."""

from __future__ import annotations

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
