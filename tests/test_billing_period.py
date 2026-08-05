"""Tests for the boundaries a stepped tariff accumulates over."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
