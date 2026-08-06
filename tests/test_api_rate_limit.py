"""Tests for reading what is left of the hourly request allowance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    RATE_LIMIT_QUERY,
    AuthenticatedGraphQLClient,
    OejpInvalidResponseError,
    async_fetch_points_allowance,
)

# The shape a real account returned: a 50,000-point hourly allowance with a Unix reset time.
REAL = {
    "limit": 50000,
    "remainingPoints": 46705,
    "usedPoints": 3295,
    "ttl": 1785916217,
    "isBlocked": False,
}


def _payload(**overrides: Any) -> dict[str, Any]:
    allowance = {**REAL, **overrides}
    return {"rateLimitInfo": {"pointsAllowanceRateLimit": allowance}}


async def test_the_allowance_is_read_from_the_shipped_document() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _payload()

    allowance = await async_fetch_points_allowance(client)

    client.execute.assert_awaited_once_with(RATE_LIMIT_QUERY)
    assert allowance.limit == 50000
    assert allowance.remaining == 46705
    assert allowance.used == 3295
    assert allowance.resets_at == datetime.fromtimestamp(1785916217, tz=UTC)
    assert allowance.blocked is False


@pytest.mark.parametrize(
    ("remaining", "blocked", "expected"),
    [
        (46705, False, False),
        (20001, False, False),
        # At the reserve, not merely below it: the reserve is what is kept, not what is spent.
        (20000, False, True),
        (46705, True, True),
    ],
)
async def test_a_discretionary_request_waits_at_the_reserve(
    remaining: int,
    blocked: bool,
    expected: bool,
) -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _payload(remainingPoints=remaining, isBlocked=blocked)

    allowance = await async_fetch_points_allowance(client)

    assert allowance.exhausted(20_000) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rateLimitInfo": None},
        {"rateLimitInfo": {"pointsAllowanceRateLimit": None}},
    ],
)
async def test_a_response_without_an_allowance_is_rejected(payload: dict[str, Any]) -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = payload

    with pytest.raises(OejpInvalidResponseError, match="points allowance"):
        await async_fetch_points_allowance(client)


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": "50000"},
        {"remainingPoints": -1},
        {"usedPoints": True},
        {"ttl": 0},
        {"ttl": "soon"},
        {"ttl": 10**20},
    ],
)
async def test_a_malformed_field_is_rejected_rather_than_coerced(
    overrides: dict[str, Any],
) -> None:
    """A wrong allowance would either stall the walk or let it spend past the limit."""
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _payload(**overrides)

    with pytest.raises(OejpInvalidResponseError):
        await async_fetch_points_allowance(client)
