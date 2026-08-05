"""Tests for the boundaries a stepped tariff accumulates over."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from custom_components.octopus_energy_japan.billing_period import (
    BillingPeriodCalendar,
    BillingPeriodSource,
)

TOKYO = ZoneInfo("Asia/Tokyo")
CALENDAR = BillingPeriodCalendar.calendar_months(TOKYO)


def test_a_period_begins_at_local_midnight_not_utc_midnight() -> None:
    """This is the whole reason the boundary cannot be a ledger partition boundary.

    A JST month begins at 15:00 UTC on the last day of the previous UTC month, so a UTC
    month boundary falls nine hours into the period. Truncating a projection there would
    restart the step counter nine hours late and misprice every later hour.
    """
    assert CALENDAR.period_start(datetime(2026, 8, 3, 3, tzinfo=UTC)) == datetime(
        2026, 7, 31, 15, tzinfo=UTC
    )


def test_an_hour_just_before_local_midnight_belongs_to_the_earlier_period() -> None:
    just_before = datetime(2026, 7, 31, 14, 30, tzinfo=UTC)
    assert CALENDAR.period_start(just_before) == datetime(2026, 6, 30, 15, tzinfo=UTC)


def test_a_moment_exactly_on_a_boundary_belongs_to_the_period_it_opens() -> None:
    boundary = datetime(2026, 7, 31, 15, tzinfo=UTC)
    assert CALENDAR.period_start(boundary) == boundary


def test_the_previous_period_start_crosses_a_year_boundary() -> None:
    """January's predecessor is the previous December, not month zero."""
    january = datetime(2027, 1, 5, tzinfo=UTC)
    assert CALENDAR.previous_period_start(january) == datetime(2026, 11, 30, 15, tzinfo=UTC)


def test_the_previous_period_start_is_the_step_before_this_one() -> None:
    moment = datetime(2026, 8, 3, 3, tzinfo=UTC)
    previous = CALENDAR.previous_period_start(moment)
    assert previous < CALENDAR.period_start(moment)
    assert CALENDAR.period_start(previous) == previous


def test_the_source_says_where_the_boundaries_came_from() -> None:
    assert CALENDAR.source is BillingPeriodSource.CALENDAR_MONTH


# 2026-06-18 00:00 JST, the supply start measured on a real account whose closed invoice
# covered 6/18 to 7/17.
SUPPLY_START = datetime(2026, 6, 17, 15, tzinfo=UTC)
ANCHORED = BillingPeriodCalendar.from_supply_start(SUPPLY_START, local_timezone=TOKYO)


def test_the_anchor_is_the_local_day_supply_began() -> None:
    """The provider reports the instant in UTC, where it is the 17th, not the 18th."""
    assert ANCHORED.anchor_day == 18
    assert ANCHORED.source is BillingPeriodSource.SUPPLY_ANCHOR


def test_the_measured_invoice_period_is_reproduced() -> None:
    """6/18 to 7/17 inclusive, which is what the closed invoice covered."""
    first_hour = datetime(2026, 6, 18, tzinfo=TOKYO)
    last_hour = datetime(2026, 7, 17, 23, 30, tzinfo=TOKYO)
    next_period = datetime(2026, 7, 18, tzinfo=TOKYO)

    assert ANCHORED.period_start(first_hour) == SUPPLY_START
    assert ANCHORED.period_start(last_hour) == SUPPLY_START
    assert ANCHORED.period_start(next_period) != SUPPLY_START


def test_the_calendar_month_boundary_is_mid_period() -> None:
    """This is the misalignment that put the wrong kilowatt-hours in the cheap first step."""
    june = datetime(2026, 6, 30, 23, 30, tzinfo=TOKYO)
    july = datetime(2026, 7, 1, tzinfo=TOKYO)

    assert ANCHORED.period_start(june) == ANCHORED.period_start(july)
    assert CALENDAR.period_start(june) != CALENDAR.period_start(july)


def test_an_anchor_the_month_is_too_short_for_is_pulled_back() -> None:
    """A supply that began on the 31st has no 31st in February.

    Clamping to the last day keeps each period adjacent to the next; dropping the month
    would leave a gap no period covered.
    """
    anchored = BillingPeriodCalendar.from_supply_start(
        datetime(2026, 1, 31, tzinfo=TOKYO),
        local_timezone=TOKYO,
    )

    february = anchored.period_start(datetime(2026, 2, 15, tzinfo=TOKYO)).astimezone(TOKYO)
    march = anchored.period_start(datetime(2026, 3, 1, tzinfo=TOKYO)).astimezone(TOKYO)

    assert (february.month, february.day) == (1, 31)
    assert (march.month, march.day) == (2, 28)


def test_a_period_start_is_its_own_period_start_for_an_anchored_calendar() -> None:
    """`period_start` has to be idempotent, because truncation feeds its output back in."""
    start = ANCHORED.period_start(datetime(2026, 7, 1, tzinfo=TOKYO))
    assert ANCHORED.period_start(start) == start
    assert ANCHORED.previous_period_start(start) < start


def test_an_impossible_anchor_is_refused() -> None:
    with pytest.raises(ValueError, match="day of the month"):
        BillingPeriodCalendar(local_timezone=TOKYO, anchor_day=0)
    with pytest.raises(ValueError, match="day of the month"):
        BillingPeriodCalendar(local_timezone=TOKYO, anchor_day=32)
