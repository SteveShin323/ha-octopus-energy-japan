"""Tests for purpose-specific OEJP GraphQL operations."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    OejpGraphQLClient,
    OejpInvalidResponseError,
    async_discover_accounts,
    async_obtain_token,
)


async def test_obtain_token_parses_all_supported_fields() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.return_value = {
        "obtainKrakenToken": {
            "token": "access",
            "refreshToken": "refresh",
            "refreshExpiresIn": "1785326400",
        }
    }

    token = await async_obtain_token(client, "user@example.com", "password")

    assert token.access_token == "access"
    assert token.refresh_token == "refresh"
    assert token.refresh_expires_at == datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    client.execute.assert_awaited_once()
    variables = client.execute.await_args.args[1]
    assert variables == {"input": {"email": "user@example.com", "password": "password"}}


async def test_obtain_token_accepts_absent_optional_fields() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.return_value = {"obtainKrakenToken": {"token": "access"}}

    token = await async_obtain_token(client, "user@example.com", "password")

    assert token.refresh_token is None
    assert token.refresh_expires_at is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"obtainKrakenToken": None},
        {"obtainKrakenToken": {}},
        {"obtainKrakenToken": {"token": ""}},
        {
            "obtainKrakenToken": {
                "token": "access",
                "refreshExpiresIn": True,
            }
        },
        {
            "obtainKrakenToken": {
                "token": "access",
                "refreshExpiresIn": "invalid",
            }
        },
        {
            "obtainKrakenToken": {
                "token": "access",
                "refreshExpiresIn": 10**30,
            }
        },
        {
            "obtainKrakenToken": {
                "token": "access",
                "refreshExpiresIn": 1.5,
            }
        },
    ],
)
async def test_obtain_token_rejects_malformed_payloads(payload: dict[str, object]) -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.return_value = payload

    with pytest.raises(OejpInvalidResponseError):
        await async_obtain_token(client, "user@example.com", "password")


async def test_discover_accounts_returns_sorted_deduplicated_accounts() -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.return_value = {
        "viewer": {
            "accounts": [
                {"number": "B-ACCOUNT", "status": "CLOSED"},
                {"number": "A-ACCOUNT", "status": "ACTIVE"},
                {"number": "B-ACCOUNT", "status": "ACTIVE"},
            ]
        }
    }

    accounts = await async_discover_accounts(client, "access")

    assert [(account.number, account.status) for account in accounts] == [
        ("A-ACCOUNT", "ACTIVE"),
        ("B-ACCOUNT", "ACTIVE"),
    ]
    client.execute.assert_awaited_once()
    assert client.execute.await_args.kwargs == {"access_token": "access"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"viewer": None},
        {"viewer": {}},
        {"viewer": {"accounts": {}}},
        {"viewer": {"accounts": [None]}},
        {"viewer": {"accounts": [{}]}},
    ],
)
async def test_discover_accounts_rejects_malformed_payloads(
    payload: dict[str, object],
) -> None:
    client = AsyncMock(spec=OejpGraphQLClient)
    client.execute.return_value = payload

    with pytest.raises(OejpInvalidResponseError):
        await async_discover_accounts(client, "access")
