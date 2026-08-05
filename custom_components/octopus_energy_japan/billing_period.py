"""The repeating boundaries a supply point's stepped charges accumulate over.

A stepped tariff prices cumulative consumption in bands, so the instant the cumulative
counter restarts decides which band each hour falls into. That instant is also where a cost
projection can be truncated without changing any result: start it on a boundary and the
counter begins clean, start it anywhere else and every later hour is priced from a partial
total.

The invoiced period runs from the day of the month supply began to the day before it in the
following month. Measured on a real account: supply began at 2026-06-18 00:00 JST and the
closed invoice covered 6/18 to 7/17. Both scheduled reading dates fell on the 18th as well.

Three other dates on the same account look like they should anchor it and do not, so none of
them is used:

* `readingDateDayOfMonth` reported 19, which matches neither boundary nor either reading date;
* `bills.fromDate` and `toDate` reported 6/17 to 7/22 with an issue date of 7/23, which is the
  document's own period, not the meter reading period;
* `statements.startAt` and `endAt` reported 6/17 to 7/23 JST, likewise.

When supply began is unknown the calendar month is used instead, which is what the code did
before this module existed.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Self

# One step back from a boundary lands in the previous period whatever its length, so the same
# expression serves the calendar month and the anchored period.
_ONE_STEP = timedelta(microseconds=1)


class BillingPeriodSource(StrEnum):
    """Where a calendar's boundaries come from."""

    CALENDAR_MONTH = "calendar_month"
    SUPPLY_ANCHOR = "supply_anchor"


@dataclass(frozen=True, slots=True)
class BillingPeriodCalendar:
    """The periods one supply point's charges accumulate over."""

    local_timezone: tzinfo
    anchor_day: int | None = None
    source: BillingPeriodSource = BillingPeriodSource.CALENDAR_MONTH

    def __post_init__(self) -> None:
        if self.anchor_day is not None and not 1 <= self.anchor_day <= 31:
            raise ValueError("A billing period anchor must be a day of the month")

    @classmethod
    def calendar_months(cls, local_timezone: tzinfo) -> Self:
        """Return the calendar that restarts on the first of each local month."""
        return cls(local_timezone=local_timezone)

    @classmethod
    def from_supply_start(cls, supply_start_at: datetime, *, local_timezone: tzinfo) -> Self:
        """Return the calendar anchored on the local day of the month supply began."""
        return cls(
            local_timezone=local_timezone,
            anchor_day=supply_start_at.astimezone(local_timezone).day,
            source=BillingPeriodSource.SUPPLY_ANCHOR,
        )

    def period_start(self, moment: datetime) -> datetime:
        """Return the instant the period containing ``moment`` began.

        Local, not UTC: a JST month begins at 15:00 UTC on the last day of the previous UTC
        month, so a UTC month boundary is nine hours late and would restart the counter
        nine hours into the period.
        """
        local = moment.astimezone(self.local_timezone)
        year, month = local.year, local.month
        if local.day < self._anchor_in(year, month):
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        start = datetime(year, month, self._anchor_in(year, month), tzinfo=self.local_timezone)
        return start.astimezone(UTC)

    def previous_period_start(self, moment: datetime) -> datetime:
        """Return the instant the period before the one containing ``moment`` began."""
        return self.period_start(self.period_start(moment) - _ONE_STEP)

    def _anchor_in(self, year: int, month: int) -> int:
        """Return the anchor day, pulled back when the month is too short to hold it.

        A supply that began on the 31st has no 31st in February. Clamping to the last day
        keeps every period adjacent to the next, which dropping the month would not.
        """
        if self.anchor_day is None:
            return 1
        return min(self.anchor_day, calendar.monthrange(year, month)[1])
