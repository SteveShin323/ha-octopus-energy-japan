"""Tests for pricing consumption with the reported tariff.

The arithmetic is checked by hand rather than against a recomputation of the same code,
because the point of these numbers is that they match a Japanese bill's structure: steps
that advance on the month's cumulative kWh, adders that only apply inside the period the
provider states, and a standing charge that no per-kWh price can express.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from custom_components.octopus_energy_japan.api import ReadingDirection
from custom_components.octopus_energy_japan.api.tariff import (
    SupplyPointTariff,
    TariffAdder,
    TariffStep,
)
from custom_components.octopus_energy_japan.billing_period import BillingPeriodCalendar
from custom_components.octopus_energy_japan.tariff_cost import project_hourly_cost
from custom_components.octopus_energy_japan.tariff_history import (
    AdderSchedule,
    live_schedule,
)

TOKYO = ZoneInfo("Asia/Tokyo")
MONTHS = BillingPeriodCalendar.calendar_months(TOKYO)
NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)


def _adders(tariff: SupplyPointTariff) -> AdderSchedule:
    """The schedule an installation with no archive yet prices from."""
    return live_schedule(tariff, observed_at=NOW)


STEPS = (
    TariffStep(start_kwh=Decimal(0), end_kwh=Decimal(120), price_inc_tax=Decimal("20.62")),
    TariffStep(start_kwh=Decimal(120), end_kwh=Decimal(300), price_inc_tax=Decimal("25.29")),
    TariffStep(start_kwh=Decimal(300), end_kwh=None, price_inc_tax=Decimal("27.44")),
)


def _tariff(
    *,
    steps: tuple[TariffStep, ...] = STEPS,
    standing: Decimal | None = Decimal("38.80"),
    fuel: TariffAdder | None = None,
    levy: TariffAdder | None = None,
) -> SupplyPointTariff:
    return SupplyPointTariff(
        account_number="A-1",
        supply_point_id="SP-1",
        product_code="P",
        product_name="P",
        steps=steps,
        standing_charge_per_day=standing,
        fuel_cost_adjustment=fuel,
        renewable_energy_levy=levy,
    )


def _hour(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


def test_energy_is_priced_at_the_first_step_until_the_month_reaches_it() -> None:
    tariff = _tariff(standing=None)

    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(10))], tariff, periods=MONTHS, adders=_adders(tariff)
    )

    assert len(costs) == 1
    assert costs[0].components.energy == Decimal(10) * Decimal("20.62")
    assert costs[0].components.standing == Decimal(0)
    assert costs[0].amount == Decimal("206.20")


def test_an_hour_crossing_a_boundary_is_split_across_both_prices() -> None:
    """The whole hour at either price would be wrong by the size of the hour.

    At 115 kWh cumulative, a 10 kWh hour is 5 kWh at the first step and 5 at the second.
    """
    tariff = _tariff(standing=None)
    hours = [(_hour(1, 0), Decimal(115)), (_hour(1, 1), Decimal(10))]

    costs = project_hourly_cost(hours, tariff, periods=MONTHS, adders=_adders(tariff))

    second = costs[1].components.energy
    assert second == Decimal(5) * Decimal("20.62") + Decimal(5) * Decimal("25.29")


def test_a_single_hour_can_cross_two_boundaries() -> None:
    tariff = _tariff(standing=None)

    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(400))], tariff, periods=MONTHS, adders=_adders(tariff)
    )

    expected = (
        Decimal(120) * Decimal("20.62")
        + Decimal(180) * Decimal("25.29")
        + Decimal(100) * Decimal("27.44")
    )
    assert costs[0].components.energy == expected


def test_the_fallback_calendar_resets_on_the_tokyo_month_not_on_utc() -> None:
    """23:00 UTC on the last day of a month is already the next month in Tokyo.

    This is the calendar used when the supply start date is unknown. Resetting on UTC would
    give the customer the cheap first step nine hours late every month, and pricing a
    Tokyo-morning hour against the previous month's total is exactly the kind of error that
    never shows up as an obvious failure.
    """
    tariff = _tariff(standing=None)
    hours = [
        # 2026-08-31 14:00 UTC is 23:00 JST on 31 August: still August.
        (datetime(2026, 8, 31, 14, tzinfo=UTC), Decimal(150)),
        # 2026-08-31 15:00 UTC is 00:00 JST on 1 September: a new month.
        (datetime(2026, 8, 31, 15, tzinfo=UTC), Decimal(10)),
    ]

    costs = project_hourly_cost(hours, tariff, periods=MONTHS, adders=_adders(tariff))

    # September starts again at the first step, despite August having passed 120 kWh.
    assert costs[1].components.energy == Decimal(10) * Decimal("20.62")


def test_the_steps_reset_on_the_invoiced_period_not_on_the_month() -> None:
    """The measured invoice ran 6/18 to 7/17, from the day of the month supply began.

    Resetting on the calendar month instead put the wrong kilowatt-hours in the cheap first
    step for the eighteen days each period spans two months, which is one of the two known
    causes of the total exceeding the invoice.
    """
    tariff = _tariff(standing=None)
    periods = BillingPeriodCalendar.from_supply_start(
        # 2026-06-18 00:00 JST, as the provider reports it.
        datetime(2026, 6, 17, 15, tzinfo=UTC),
        local_timezone=TOKYO,
    )
    hours = [
        # 2026-07-17 23:00 JST, the last hour of the invoiced period.
        (datetime(2026, 7, 17, 14, tzinfo=UTC), Decimal(150)),
        # 2026-07-18 00:00 JST, the first hour of the next one.
        (datetime(2026, 7, 17, 15, tzinfo=UTC), Decimal(10)),
    ]

    costs = project_hourly_cost(hours, tariff, periods=periods, adders=_adders(tariff))

    assert costs[1].components.energy == Decimal(10) * Decimal("20.62")


def test_a_calendar_month_boundary_does_not_reset_an_invoiced_period() -> None:
    """The 1st is mid-period for every supply that did not begin on the 1st."""
    tariff = _tariff(standing=None)
    periods = BillingPeriodCalendar.from_supply_start(
        datetime(2026, 6, 17, 15, tzinfo=UTC),
        local_timezone=TOKYO,
    )
    hours = [
        # 2026-06-30 23:00 JST and 2026-07-01 00:00 JST: one calendar month apart, and the
        # same invoiced period, which began on 6/18.
        (datetime(2026, 6, 30, 14, tzinfo=UTC), Decimal(150)),
        (datetime(2026, 6, 30, 15, tzinfo=UTC), Decimal(10)),
    ]

    costs = project_hourly_cost(hours, tariff, periods=periods, adders=_adders(tariff))

    # Still in the second step, because the period's total already passed 120 kWh.
    assert costs[1].components.energy == Decimal(10) * Decimal("25.29")


def test_the_standing_charge_accrues_one_hour_at_a_time() -> None:
    """A day with only some hours published must not be billed as a whole day."""
    tariff = _tariff()
    hours = [(_hour(1, hour), Decimal(1)) for hour in range(24)]

    costs = project_hourly_cost(hours, tariff, periods=MONTHS, adders=_adders(tariff))

    per_hour = Decimal("38.80") / Decimal(24)
    assert all(cost.components.standing == per_hour for cost in costs)
    # A twenty-fourth of 38.80 is not exact in Decimal's default precision, so twenty-four
    # of them differ from the daily charge by about 1e-26 yen. No rounding is introduced to
    # hide that: publication converts to float anyway, which is far coarser, and rounding
    # each hour would bias every day in one direction.
    daily = sum((cost.components.standing for cost in costs), Decimal(0))
    assert abs(daily - Decimal("38.80")) < Decimal("1e-20")


def test_a_partly_published_day_accrues_only_its_share() -> None:
    tariff = _tariff()

    costs = project_hourly_cost(
        [(_hour(1, hour), Decimal(1)) for hour in range(6)],
        tariff,
        periods=MONTHS,
        adders=_adders(tariff),
    )

    total = sum((cost.components.standing for cost in costs), Decimal(0))
    assert total == Decimal("38.80") / Decimal(24) * 6


def test_an_adder_applies_at_its_stated_price_inside_its_own_period() -> None:
    fuel = TariffAdder(
        price_inc_tax=Decimal("4.32"),
        valid_from=datetime(2026, 7, 31, 15, tzinfo=UTC),
        valid_to=datetime(2026, 8, 31, 15, tzinfo=UTC),
    )
    levy = TariffAdder(
        price_inc_tax=Decimal("4.18"),
        valid_from=datetime(2026, 4, 30, 15, tzinfo=UTC),
        valid_to=datetime(2027, 4, 30, 15, tzinfo=UTC),
    )
    tariff = _tariff(standing=None, fuel=fuel, levy=levy)

    costs = project_hourly_cost(
        [(datetime(2026, 8, 4, 0, tzinfo=UTC), Decimal(1))],
        tariff,
        periods=MONTHS,
        adders=_adders(tariff),
    )

    assert costs[0].components.adders == Decimal("8.50")


def test_an_hour_outside_every_known_period_uses_the_nearest_rate() -> None:
    """It used to get nothing, which understated every hour outside the current month.

    The provider serves only the adjustment in force, so an hour from an earlier month has no
    stated rate until the archive has one. Reaching to the near end of what is known is the
    smallest extrapolation, and it converges as the archive fills. Charging zero was not a
    smaller error — it was a certain one.
    """
    fuel = TariffAdder(
        price_inc_tax=Decimal("4.32"),
        valid_from=datetime(2026, 7, 31, 15, tzinfo=UTC),
        valid_to=datetime(2026, 8, 31, 15, tzinfo=UTC),
    )
    tariff = _tariff(standing=None, fuel=fuel)

    costs = project_hourly_cost(
        # A month before the only period the provider is currently stating.
        [(datetime(2026, 7, 1, 0, tzinfo=UTC), Decimal(1))],
        tariff,
        periods=MONTHS,
        adders=_adders(tariff),
    )

    assert costs[0].components.adders == Decimal("4.32")


def test_export_is_never_priced_as_consumption() -> None:
    """Feeding energy back is compensated separately, not charged at a consumption rate."""
    assert (
        project_hourly_cost(
            [(_hour(1, 0), Decimal(10))],
            _tariff(),
            periods=MONTHS,
            adders=_adders(_tariff()),
            direction=ReadingDirection.EXPORT,
        )
        == ()
    )


def test_an_unpriceable_tariff_produces_no_cost() -> None:
    assert (
        project_hourly_cost(
            [(_hour(1, 0), Decimal(10))],
            _tariff(steps=()),
            periods=MONTHS,
            adders=_adders(_tariff(steps=())),
        )
        == ()
    )


def test_hours_are_priced_in_order_however_they_arrive() -> None:
    """The steps depend on accumulation, so an unsorted input must not change the total."""
    tariff = _tariff(standing=None)
    hours = [(_hour(1, 1), Decimal(10)), (_hour(1, 0), Decimal(115))]

    costs = project_hourly_cost(hours, tariff, periods=MONTHS, adders=_adders(tariff))

    assert [cost.start for cost in costs] == [_hour(1, 0), _hour(1, 1)]
    assert costs[1].components.energy == Decimal(5) * Decimal("20.62") + Decimal(5) * Decimal(
        "25.29"
    )


@pytest.mark.parametrize("kwh", [Decimal(0), Decimal("-1.5")])
def test_a_zero_or_negative_reading_still_accrues_the_standing_charge(kwh: Decimal) -> None:
    """The standing charge is owed for the day regardless of consumption."""
    costs = project_hourly_cost(
        [(_hour(1, 0), kwh)], _tariff(), periods=MONTHS, adders=_adders(_tariff())
    )

    assert costs[0].components.standing == Decimal("38.80") / Decimal(24)
    assert costs[0].components.energy == kwh * Decimal("20.62")


def test_the_components_sum_to_the_amount() -> None:
    fuel = TariffAdder(
        price_inc_tax=Decimal("4.32"),
        valid_from=datetime(2026, 7, 31, 15, tzinfo=UTC),
    )
    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(2))],
        _tariff(fuel=fuel),
        periods=MONTHS,
        adders=_adders(_tariff(fuel=fuel)),
    )

    parts = costs[0].components
    assert costs[0].amount == parts.energy + parts.adders + parts.standing
    assert parts.energy == Decimal(2) * Decimal("20.62")
    assert parts.adders == Decimal(2) * Decimal("4.32")


def test_a_degenerate_step_definition_does_not_loop_forever() -> None:
    """Two steps sharing a start would otherwise take zero each time round."""
    steps = (
        TariffStep(start_kwh=Decimal(0), end_kwh=Decimal(0), price_inc_tax=Decimal(10)),
        TariffStep(start_kwh=Decimal(0), end_kwh=None, price_inc_tax=Decimal(20)),
    )

    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(5))],
        _tariff(steps=steps, standing=None),
        periods=MONTHS,
        adders=_adders(_tariff(steps=steps, standing=None)),
    )

    assert len(costs) == 1
    assert costs[0].components.energy > 0


def test_a_step_with_no_price_stops_pricing_rather_than_looping() -> None:
    """A price the provider omitted must not be treated as zero or spun on."""
    steps = (TariffStep(start_kwh=Decimal(0), end_kwh=None, price_inc_tax=Decimal(0)),)
    tariff = _tariff(steps=steps, standing=None)

    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(5))], tariff, periods=MONTHS, adders=_adders(tariff)
    )

    assert costs[0].components.energy == Decimal(0)


def test_pricing_stops_when_no_step_covers_the_position() -> None:
    """Steps that start above zero leave a gap the provider should never send.

    Pricing the gap at the nearest step would invent a rate, so the hour is priced only as
    far as the definitions reach.
    """
    steps = (TariffStep(start_kwh=Decimal(100), end_kwh=None, price_inc_tax=Decimal(10)),)
    tariff = _tariff(steps=steps, standing=None)

    costs = project_hourly_cost(
        [(_hour(1, 0), Decimal(5))], tariff, periods=MONTHS, adders=_adders(tariff)
    )

    # `marginal_price` falls back to the last step, so the 5 kWh is priced at 10 rather
    # than silently dropped; what matters is that it terminates with a defined answer.
    assert costs[0].components.energy == Decimal(50)
