"""What the provider says is left of this hour's request allowance.

Measured against a real account: a reading request costs a flat 17 points whatever page size it
asks for, `rateLimitInfo` itself costs 5, and the allowance is 50,000 per hour for an account
user. A long walk into the past is the only work here that could plausibly spend that, so it is
the only work that reads this — everything else is bounded by its own cadence.

Asking is cheaper than guessing. A fixed interval chosen from the numbers above would still be
wrong on an account that is spending its allowance somewhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from .auth import AuthenticatedGraphQLClient
from .errors import OejpInvalidResponseError

RATE_LIMIT_QUERY: Final = """
query OejpRateLimit {
  rateLimitInfo {
    pointsAllowanceRateLimit {
      limit
      remainingPoints
      usedPoints
      ttl
      isBlocked
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class PointsAllowance:
    """One reading of the hourly point allowance."""

    limit: int
    remaining: int
    used: int
    # When the allowance resets. The provider reports it as a Unix timestamp.
    resets_at: datetime
    blocked: bool

    def exhausted(self, reserve: int) -> bool:
        """Report whether a discretionary request should wait for the reset."""
        return self.blocked or self.remaining <= reserve


async def async_fetch_points_allowance(
    client: AuthenticatedGraphQLClient,
) -> PointsAllowance:
    """Return what is left of this hour's allowance."""
    data = await client.execute(RATE_LIMIT_QUERY)
    info = data.get("rateLimitInfo")
    allowance = info.get("pointsAllowanceRateLimit") if isinstance(info, Mapping) else None
    if not isinstance(allowance, Mapping):
        raise OejpInvalidResponseError("Rate limit response did not contain a points allowance")
    return PointsAllowance(
        limit=_count(allowance, "limit"),
        remaining=_count(allowance, "remainingPoints"),
        used=_count(allowance, "usedPoints"),
        resets_at=_epoch(allowance.get("ttl")),
        blocked=allowance.get("isBlocked") is True,
    )


def _count(allowance: Mapping[str, Any], key: str) -> int:
    value = allowance.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OejpInvalidResponseError(f"Rate limit {key} was malformed")
    return value


def _epoch(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise OejpInvalidResponseError("Rate limit reset time was malformed")
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError) as err:
        raise OejpInvalidResponseError("Rate limit reset time was malformed") from err
