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
calendar month when it reports none.

A time-of-use tariff replaces the first line rather than adding to it: the price depends on the
hour, not on how much has been consumed so far. The provider sells no tariff that does both —
every time-of-use product returns its rates without step boundaries — so the two are exclusive
here as well. Which hours each band covers comes from `api/tou.py`, because the provider
publishes them in its tariff documents and refuses every query that would return them.

Which rates apply is the provider's statement too, not an assumption: an hour is priced with the
rate generation whose validity window covers it.

A supply point whose agreement has ended is priced from that agreement's own rates —
`SupplyPointTariff.is_estimate` says so — but the two adders can still only be read from
whatever the archive holds. An hour the archive never saw live falls back to the nearest
value it does hold, which `HourlyCost.adders_extrapolated` records so the caller can count how
much of a bill is approximated rather than measured.

Every price the provider gives is available with and without tax. The tax-inclusive one is
used throughout: it is what the customer pays, and a cost shown beside consumption that
excluded tax would understate the bill by ten percent.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from .api import ReadingDirection
from .api.tariff import SupplyPointTariff, TariffStep
from .api.tou import scheme_for, slot_at
from .billing_period import BillingPeriodCalendar
from .tariff_history import AdderSchedule

# One hour's share of a daily standing charge. A day with only some hours published
# accrues only that share, and the rest arrives with the remaining readings, so a
# part-synchronised day is never billed as a whole one.
HOURS_PER_DAY: Final = Decimal(24)

_LOGGER = logging.getLogger(__name__)


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
    # True when the fuel-cost adjustment or the renewable levy priced into this hour was not
    # the rate that actually applied then — the archive held no record covering it, so the
    # nearest one was used instead. Counted by the caller so a cost built from an
    # approximation is not indistinguishable from one that is exact.
    adders_extrapolated: bool = False

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

        if tariff.is_time_of_use:
            price = _band_price(tariff, moment)
            if price is None:
                # The hour resolved to a slot the agreement did not price. Charging it at
                # nothing would understate the period, so the whole supply point is dropped
                # rather than part-priced.
                #
                # Parsing already refuses a tariff whose bands do not cover its scheme, so
                # this should be unreachable. If it is reached the caller cannot tell the
                # result from a batch with no hours in it, which is what the log is for.
                _LOGGER.warning(
                    "No time-of-use price for %s at %s; no cost will be published for "
                    "this supply point. Please report this with your diagnostics",
                    tariff.tou_scheme,
                    moment.isoformat(),
                )
                return ()
            energy = kwh * price
        else:
            energy = _price_across_steps(tariff.steps_at(moment), cumulative, kwh)
        cumulative_by_period[period] = cumulative + kwh
        adder_rate = adders.rate_at(moment)

        costs.append(
            HourlyCost(
                start=moment,
                components=CostComponents(
                    energy=energy,
                    adders=kwh * adder_rate.total,
                    standing=standing_per_hour,
                ),
                adders_extrapolated=adder_rate.extrapolated,
            )
        )
    return tuple(costs)


def _band_price(tariff: SupplyPointTariff, moment: datetime) -> Decimal | None:
    """Return the per-kWh price of the time-of-use band covering this hour.

    Every boundary in every scheme falls on a whole hour and Japan keeps a fixed offset from
    UTC, so a UTC hour lies wholly inside one band and never has to be split the way an hour
    crossing a step boundary does.
    """
    scheme = scheme_for(tariff.tou_scheme)
    if scheme is None or tariff.grid_operator_code is None:
        return None
    slot = slot_at(scheme, tariff.grid_operator_code, moment)
    if slot is None:
        return None
    return tariff.price_for_slot(slot, moment)


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
