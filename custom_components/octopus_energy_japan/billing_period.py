"""The repeating boundaries a supply point's stepped charges accumulate over.

A stepped tariff prices cumulative consumption in bands, so the instant the cumulative
counter restarts decides which band each hour falls into. That instant is also where a cost
projection can be truncated without changing any result: start it on a boundary and the
counter begins clean, start it anywhere else and every later hour is priced from a partial
total.

Only the local calendar month is modelled here. It is what the code has always used, and it
matches the provider's own rate validity windows, which are JST calendar months.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Self

# One step back from a boundary lands in the previous period whatever its length, so the
# same expression works for a calendar month and for any anchored period added later.
_ONE_STEP = timedelta(microseconds=1)


class BillingPeriodSource(StrEnum):
    """Where a calendar's boundaries come from."""

    CALENDAR_MONTH = "calendar_month"


@dataclass(frozen=True, slots=True)
class BillingPeriodCalendar:
    """The periods one supply point's charges accumulate over."""

    local_timezone: tzinfo
    source: BillingPeriodSource = BillingPeriodSource.CALENDAR_MONTH

    @classmethod
    def calendar_months(cls, local_timezone: tzinfo) -> Self:
        """Return the calendar that restarts on the first of each local month."""
        return cls(local_timezone=local_timezone)

    def period_start(self, moment: datetime) -> datetime:
        """Return the instant the period containing ``moment`` began.

        Local, not UTC: a JST month begins at 15:00 UTC on the last day of the previous UTC
        month, so a UTC month boundary is nine hours late and would restart the counter
        nine hours into the period.
        """
        local = moment.astimezone(self.local_timezone)
        start = datetime(local.year, local.month, 1, tzinfo=self.local_timezone)
        return start.astimezone(UTC)

    def previous_period_start(self, moment: datetime) -> datetime:
        """Return the instant the period before the one containing ``moment`` began."""
        return self.period_start(self.period_start(moment) - _ONE_STEP)
