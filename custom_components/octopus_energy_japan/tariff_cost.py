"""Price hourly consumption with the tariff the provider reports.

A Japanese electricity bill is not consumption times a rate. It is:

    energy   — kWh priced by which step the *billing period's cumulative* kWh has reached
    adders   — kWh times the fuel-cost adjustment and levy that applied at that hour
    standing — a fixed charge per day, independent of consumption

Only the first depends on the reading alone. The second needs the moment, because the fuel
adjustment changes monthly, the provider states the month it covers, and it serves only the
one in force — `tariff_history.py` keeps the rest. The third
depends on nothing but the calendar, which is why a per-kWh price can never express it and
why this is computed here rather than handed to Home Assistant as a unit price.

Steps advance on the cumulative total for one **billing period**, which the caller supplies as
a `BillingPeriodCalendar` — anchored on the meter-reading day the account reports, or the local
calendar month when it reports none. Everything else in this integration works in UTC hours, so
the conversion to local time happens inside that calendar and nowhere else.

Which rates apply is the provider's statement too, not an assumption: an hour is priced with the
rate generation whose validity window covers it.

Every price the provider gives is available with and without tax. The tax-inclusive one is
used throughout: it is what the customer pays, and a cost shown beside consumption that
excluded tax would understate the bill by ten percent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from .api import ReadingDirection
from .api.tariff import SupplyPointTariff, TariffStep
from .billing_period import BillingPeriodCalendar
from .tariff_history import AdderSchedule

# One hour's share of a daily standing charge. A day with only some hours published
# accrues only that share, and the rest arrives with the remaining readings, so a
# part-synchronised day is never billed as a whole one.
HOURS_PER_DAY: Final = Decimal(24)


@dataclass(frozen=True, slots=True)
class CostComponents:
    """What a projected cost is made of, for diagnostics and for explaining a total."""

    energy: Decimal
    adders: Decimal
    standing: Decimal

    @property
    def total(self) -> Decimal:
        return self.energy + self.adders + self.standing


@dataclass(frozen=True, slots=True)
class HourlyCost:
    """One UTC hour's cost, and the parts it came from."""

    start: datetime
    components: CostComponents

    @property
    def amount(self) -> Decimal:
        return self.components.total


def project_hourly_cost(
    energy_hours: Sequence[tuple[datetime, Decimal]],
    tariff: SupplyPointTariff,
    *,
    periods: BillingPeriodCalendar,
    adders: AdderSchedule,
    direction: ReadingDirection = ReadingDirection.IMPORT,
) -> tuple[HourlyCost, ...]:
    """Price each hour of consumption, in provider currency.

    `energy_hours` must be UTC hour starts with the kWh attributed to that hour. Order is
    not assumed; the steps need chronological accumulation, so the input is sorted here.

    Export is never priced. Feeding energy back is compensated under a different
    arrangement than consumption, and applying a consumption tariff to it would invent a
    charge the customer does not owe.
    """
    if direction is not ReadingDirection.IMPORT:
        return ()
    if not tariff.is_priceable:
        return ()

    cumulative_by_period: dict[datetime, Decimal] = {}
    standing_per_hour = (
        tariff.standing_charge_per_day / HOURS_PER_DAY
        if tariff.standing_charge_per_day is not None
        else Decimal(0)
    )

    costs: list[HourlyCost] = []
    for hour, kwh in sorted(energy_hours, key=lambda item: item[0]):
        moment = hour.astimezone(UTC)
        period = periods.period_start(moment)
        cumulative = cumulative_by_period.get(period, Decimal(0))

        energy = _price_across_steps(tariff.steps_at(moment), cumulative, kwh)
        cumulative_by_period[period] = cumulative + kwh
        addition = kwh * adders.rate_at(moment).total

        costs.append(
            HourlyCost(
                start=moment,
                components=CostComponents(
                    energy=energy,
                    adders=addition,
                    standing=standing_per_hour,
                ),
            )
        )
    return tuple(costs)


def _price_across_steps(
    steps: tuple[TariffStep, ...],
    cumulative_kwh: Decimal,
    kwh: Decimal,
) -> Decimal:
    """Price one hour's kWh, splitting it if it crosses a step boundary.

    An hour that takes the period past a step boundary is partly at the lower price and
    partly at the higher one. Pricing the whole hour at either would be wrong by the size of
    the hour, every period, at the boundary.
    """
    if kwh <= 0:
        price = _marginal_price(steps, cumulative_kwh)
        return kwh * price if price is not None else Decimal(0)

    remaining = kwh
    position = cumulative_kwh
    total = Decimal(0)
    while remaining > 0:
        price = _marginal_price(steps, position)
        if price is None:
            break
        boundary = _next_boundary(steps, position)
        take = remaining if boundary is None else min(remaining, boundary - position)
        if take <= 0:
            # A degenerate step definition would otherwise spin here.
            take = remaining
        total += take * price
        position += take
        remaining -= take
    return total


def _marginal_price(steps: tuple[TariffStep, ...], cumulative_kwh: Decimal) -> Decimal | None:
    """Return the price of the next kWh at this period-cumulative total."""
    for step in steps:
        if step.contains(cumulative_kwh):
            return step.price_inc_tax
    return steps[-1].price_inc_tax if steps else None


def _next_boundary(steps: Iterable[TariffStep], position: Decimal) -> Decimal | None:
    """Return the next step start strictly above `position`, if any."""
    upcoming = [step.start_kwh for step in steps if step.start_kwh > position]
    return min(upcoming) if upcoming else None
