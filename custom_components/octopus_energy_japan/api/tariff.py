"""The tariff a supply point is actually charged on.

Everything a Japanese electricity bill is built from is readable by an account user, on
the agreement's product. Confirmed field by field against one real account on 2026-08-04
and recorded in `docs/API_CONTRACTS.md`:

- `consumptionCharges` — the energy price. On `ElectricitySteppedProduct` each entry carries
  its kWh step boundaries; on `ElectricitySingleStepProduct` there are none, because one
  price covers everything;
- `standingChargePricePerDay` — a per-day figure, and already resolved for the customer's
  contract. `standingChargeUnitType` reads `YEN_AMPERE_DAY` on the one account measured,
  which describes how the charge is *determined* — by contracted amperage, per day — not
  the unit of the number returned: the figure equalled the per-day amount that account's
  published tariff table lists for its contracted amperage, and the same amount appeared on
  its invoice as the daily basic charge. A per-ampere rate would have been that divided by
  the amperage. So it is used as a daily amount, and the unit is still reported in
  diagnostics so an account whose plan resolves it differently can be traced;
- `fuelCostAdjustment` — the monthly 燃料費調整, carrying the month it is valid for; and
- `renewableEnergyLevy` — the annual 再エネ賦課金.

Nothing here assumes a region, an operator, or a plan shape. Rates arrive scoped to the
agreement, and a shape this formula cannot express is refused with a recorded reason rather
than approximated.

Three earlier attempts concluded these were unreachable. Each searched for a field whose
declared type was `ProductInterface` or one of its members and found none. The declared
type is the union `Product`; the members are reached through an inline fragment. That is
why this module exists as its own file: the path is not obvious and the reasoning should
not be buried.

Prices come in both `pricePerUnit` and `pricePerUnitIncTax`. The tax-inclusive figures are
used, because that is what a customer is billed and what a cost shown next to consumption
should mean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from .auth import AuthenticatedGraphQLClient
from .errors import OejpInvalidResponseError
from .tou import scheme_for, split_band

# An account user reads this through `ElectricitySupplyPoint.agreements`, not through
# `marketSupplyAgreements`: the latter's `product.rates` is refused with `KT-CT-1111` and
# the refusal nulls the product with it.
SUPPLY_POINT_TARIFF_QUERY: Final = """
query SupplyPointTariff($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    ... on Account {
      properties {
        electricitySupplyPoints {
          id
          spin
          agreements {
            id
            validFrom
            validTo
            isRevoked
            product {
              __typename
              ... on ElectricitySteppedProduct {
                code
                displayName
                params
                standingChargeUnitType
                standingChargePricePerDay
                consumptionCharges {
                  band
                  stepStart
                  stepEnd
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  timeOfUse
                  gridOperatorCode
                  regionOfOperation
                  validFrom
                  validTo
                }
                fuelCostAdjustment {
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  validFrom
                  validTo
                }
                renewableEnergyLevy {
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  validFrom
                  validTo
                }
              }
              ... on ElectricitySingleStepProduct {
                code
                displayName
                params
                standingChargeUnitType
                standingChargePricePerDay
                consumptionCharges {
                  band
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  timeOfUse
                  gridOperatorCode
                  regionOfOperation
                  validFrom
                  validTo
                }
                fuelCostAdjustment {
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  validFrom
                  validTo
                }
                renewableEnergyLevy {
                  pricePerUnit
                  pricePerUnitIncTax
                  unitType
                  validFrom
                  validTo
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
# Names the time-of-use schedule a product follows, for the case where the agreement's own
# `params` arrives empty. This surface needs no entitlement to the product: it was read for the
# EV product from an account on a stepped tariff, and `productCode` is optional — omitting it
# returns the whole catalogue for the area.
TARIFF_SUMMARY_QUERY: Final = """
query TariffSummary($gridOperatorCode: String!, $productCode: String) {
  tariffSummary(gridOperatorCode: $gridOperatorCode, productCode: $productCode) {
    code
    productParams
  }
}
"""

# `agreements` is a plain list on this type, not a connection, so it takes no `first`.
# The conformance scan in `tests/test_api_conformance.py` recognises connections by their
# `edges` child and therefore leaves it alone.

# Only per-kWh consumption is modelled. Anything else — capacity charges, demand charges —
# would need a different formula, and pretending otherwise would produce a confident wrong
# number, so an unexpected unit makes the tariff unusable rather than approximate.
CONSUMPTION_UNIT: Final = "KWH_CONSUMPTION"

# `Product` is a union. Only these two members price consumption: `ElectricityFitProduct`
# carries `generationCredits` for exported energy and no consumption charges at all, and
# `GasTieredProduct` is gas, which this integration does not read. Both were confirmed as
# union members by introspection.
_STEPPED_PRODUCT: Final = "ElectricitySteppedProduct"
_SINGLE_STEP_PRODUCT: Final = "ElectricitySingleStepProduct"
_CONSUMPTION_PRODUCTS: Final = frozenset({_STEPPED_PRODUCT, _SINGLE_STEP_PRODUCT})

# Sorts a rate generation with no stated start before every dated one.
_DISTANT_PAST: Final = datetime.min.replace(tzinfo=UTC)


class TariffUnpriceable(StrEnum):
    """Why a product's consumption cannot be priced by this formula.

    Carried rather than discarded so the reason can reach diagnostics and a repair issue. A
    missing cost statistic with no explanation is indistinguishable from a defect.
    """

    # A charge measured in something other than consumed kWh, such as a capacity or demand
    # charge. Pricing the rest of the tariff without it would understate every hour.
    UNSUPPORTED_UNIT = "unsupported_unit"
    # A tariff whose price varies by time of day, under a scheme `tou.py` does not carry the
    # hours for. The provider names the scheme but never publishes its hours to a customer, so
    # an unknown one cannot be priced at all.
    TIME_OF_USE_SCHEME_UNKNOWN = "time_of_use_scheme_unknown"
    # A known scheme, but the agreement did not price every band the scheme defines for this
    # area. The hours with no price would have to be charged at nothing, which would understate
    # every bill they fall in.
    TIME_OF_USE_BANDS_INCOMPLETE = "time_of_use_bands_incomplete"
    # Charges from more than one grid operator or region, which cannot be one step ladder.
    MIXED_OPERATOR = "mixed_operator"
    # A consumption product that returned no usable charge.
    NO_CONSUMPTION_CHARGES = "no_consumption_charges"
    # Consumption agreements exist on this supply point but none is in force: every one is
    # revoked or has ended, and nothing has replaced it. A customer mid-switch, or one who has
    # moved out with the entry still installed.
    #
    # Distinguished from having no consumption agreement at all, which is an export-only or
    # gas-only point and not a problem to report. Both leave the cost statistic absent, and
    # without the distinction the second silences the first.
    AGREEMENT_LAPSED = "agreement_lapsed"


@dataclass(frozen=True, slots=True)
class TariffStep:
    """One step of a stepped consumption charge, priced per kWh including tax."""

    start_kwh: Decimal
    end_kwh: Decimal | None
    price_inc_tax: Decimal
    price_ex_tax: Decimal | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def contains(self, cumulative_kwh: Decimal) -> bool:
        """Report whether the next kWh at this cumulative total falls in this step."""
        if cumulative_kwh < self.start_kwh:
            return False
        return self.end_kwh is None or cumulative_kwh < self.end_kwh


@dataclass(frozen=True, slots=True)
class TariffBand:
    """One time-of-use band's price, and the slot of the scheme it belongs to.

    `slot` is the band with its grid operator code and capacity-tier marker removed, which is
    what `tou.py` keys its schedules on. `band` is kept verbatim for diagnostics: a mismatch
    between what the provider sent and what was matched is otherwise invisible.
    """

    slot: str
    band: str
    price_inc_tax: Decimal
    price_ex_tax: Decimal | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class TariffAdder:
    """A flat per-kWh addition with the period the provider says it applies to."""

    price_inc_tax: Decimal
    price_ex_tax: Decimal | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def applies_at(self, moment: datetime) -> bool:
        """Report whether this rate covers the given moment.

        A rate with no stated bounds is treated as current, because the provider omits
        them on rates it considers open-ended rather than to signal absence.
        """
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to


@dataclass(frozen=True, slots=True)
class SupplyPointTariff:
    """Everything needed to price one supply point's consumption."""

    account_number: str
    supply_point_id: str
    product_code: str | None
    product_name: str | None
    steps: tuple[TariffStep, ...]
    standing_charge_per_day: Decimal | None
    fuel_cost_adjustment: TariffAdder | None
    renewable_energy_levy: TariffAdder | None
    # What the provider says the standing charge is measured in. Reported rather than acted
    # on: one real account returned `YEN_AMPERE_DAY`, so the set of possible values is not
    # known and an allow-list built from one account would refuse valid tariffs. Surfaced in
    # diagnostics so a reported cost mismatch carries the structure it came from.
    standing_charge_unit: str | None = None
    product_type: str | None = None
    unpriceable_reason: TariffUnpriceable | None = None
    # A time-of-use tariff prices by the hour instead of by cumulative kWh, so it carries
    # bands where a stepped one carries steps. The two never appear together: every
    # time-of-use product the provider sells reports `contractCapacityPattern` with no step
    # boundaries at all, measured across all nine grid areas on 2026-08-13.
    bands: tuple[TariffBand, ...] = ()
    # The provider's own name for the schedule those bands follow, from `params`, and the grid
    # area it applies in. Both are needed to look the hours up.
    tou_scheme: str | None = None
    grid_operator_code: str | None = None

    @property
    def is_time_of_use(self) -> bool:
        return bool(self.bands)

    @property
    def is_priceable(self) -> bool:
        """Report whether a cost can be computed at all.

        The energy price is the irreducible part: steps for a tariff that charges by
        cumulative consumption, bands for one that charges by the hour. The standing charge
        and the two adders are each optional, and their absence lowers the result rather than
        invalidating it, which the projector records.

        A recorded refusal overrides all of it. A time-of-use tariff whose scheme could not be
        named keeps its bands — they are what a second attempt at naming the scheme needs —
        but the prices in them cannot be placed in time, so it is not priceable.
        """
        if self.unpriceable_reason is not None:
            return False
        return bool(self.steps or self.bands)

    def steps_at(self, moment: datetime) -> tuple[TariffStep, ...]:
        """Return the step ladder the provider says was in force at ``moment``.

        The provider may publish more than one generation of rates, each with its own validity
        window. Merging them builds one ladder out of two tariffs: the boundaries repeat and
        which price applies depends on sort order rather than on the date. Selecting the
        generation that covers the moment prices each hour with the rates that applied then.

        A moment no generation covers is priced with the last generation that had begun by then,
        which is the one whose prices were most recently in force. Before any of them had begun
        the earliest is used. Refusing to price those hours would leave holes in the cost series,
        and carrying the last known price forward is a smaller error than reaching past a gap for
        a later one. This is the same rule the stored fuel-cost adjustments follow.
        """
        return _generation_at(self.steps, moment)

    def bands_at(self, moment: datetime) -> tuple[TariffBand, ...]:
        """Return the time-of-use bands the provider says were in force at ``moment``.

        Bands are published in generations exactly as steps are — one real agreement returns
        every band stamped with the same `validFrom` — so they are selected the same way, and
        for the same reason: merging two generations would leave the price of a slot decided
        by sort order rather than by the date.
        """
        return _generation_at(self.bands, moment)

    def marginal_price(self, cumulative_kwh: Decimal, moment: datetime) -> Decimal | None:
        """Return the price of the next kWh at this period-cumulative total."""
        steps = self.steps_at(moment)
        for step in steps:
            if step.contains(cumulative_kwh):
                return step.price_inc_tax
        return steps[-1].price_inc_tax if steps else None

    def adders_at(self, moment: datetime) -> Decimal:
        """Return the combined per-kWh additions that apply at this moment."""
        total = Decimal(0)
        for adder in (self.fuel_cost_adjustment, self.renewable_energy_levy):
            if adder is not None and adder.applies_at(moment):
                total += adder.price_inc_tax
        return total


def _generation_at[RateT: (TariffStep, TariffBand)](
    rates: tuple[RateT, ...],
    moment: datetime,
) -> tuple[RateT, ...]:
    """Return the generation of rates whose validity window covers ``moment``.

    A moment no generation covers is priced with the last generation that had begun by then,
    which is the one whose prices were most recently in force. Before any of them had begun the
    earliest is used. Refusing to price those hours would leave holes in the cost series, and
    carrying the last known price forward is a smaller error than reaching past a gap for a
    later one. This is the same rule the stored fuel-cost adjustments follow.
    """
    windows: dict[tuple[datetime | None, datetime | None], list[RateT]] = {}
    for rate in rates:
        windows.setdefault((rate.valid_from, rate.valid_to), []).append(rate)
    if len(windows) <= 1:
        return rates

    ordered = sorted(windows.items(), key=lambda item: item[0][0] or _DISTANT_PAST)
    started: list[RateT] | None = None
    for (valid_from, valid_to), generation in ordered:
        if valid_from is not None and valid_from > moment:
            break
        started = generation
        if valid_to is None or moment < valid_to:
            return tuple(generation)
    return tuple(started if started is not None else ordered[0][1])


async def async_fetch_supply_point_tariffs(
    client: AuthenticatedGraphQLClient,
    account_number: str,
) -> tuple[SupplyPointTariff, ...]:
    """Read the active tariff for every electricity supply point on one account."""
    result = await client.execute_optional(
        SUPPLY_POINT_TARIFF_QUERY,
        {"accountNumber": account_number},
    )
    if result.data is None:
        return ()
    tariffs = parse_supply_point_tariffs(result.data, account_number)
    return await _async_name_missing_schemes(client, tariffs)


async def _async_name_missing_schemes(
    client: AuthenticatedGraphQLClient,
    tariffs: tuple[SupplyPointTariff, ...],
) -> tuple[SupplyPointTariff, ...]:
    """Fill in scheme identifiers the agreement's `params` did not carry.

    `tariffSummary` answers for any product code in a grid area without the account being on
    it — it was read for the EV product from an account on a stepped tariff — so it can name
    the schedule when `params` comes back empty. Only tariffs refused for want of a name are
    retried, and a failure leaves the refusal in place rather than raising: a cost that cannot
    be computed is not a reason to fail the whole account refresh.
    """
    pending: set[tuple[str, str]] = set()
    for tariff in tariffs:
        if tariff.unpriceable_reason is not TariffUnpriceable.TIME_OF_USE_SCHEME_UNKNOWN:
            continue
        if tariff.tou_scheme is not None:
            # Named, but named something with no transcribed hours. Asking again would return
            # the same name.
            continue
        if tariff.grid_operator_code is not None and tariff.product_code is not None:
            pending.add((tariff.grid_operator_code, tariff.product_code))
    if not pending:
        return tariffs

    named: dict[tuple[str, str], str] = {}
    for grid_operator_code, product_code in sorted(pending):
        identifier = await _async_scheme_identifier(client, grid_operator_code, product_code)
        if identifier is not None:
            named[(grid_operator_code, product_code)] = identifier
    if not named:
        return tariffs

    resolved: list[SupplyPointTariff] = []
    for tariff in tariffs:
        identifier = None
        if (
            tariff.unpriceable_reason is TariffUnpriceable.TIME_OF_USE_SCHEME_UNKNOWN
            and tariff.grid_operator_code is not None
            and tariff.product_code is not None
        ):
            identifier = named.get((tariff.grid_operator_code, tariff.product_code))
        resolved.append(resolve_time_of_use(tariff, identifier) if identifier else tariff)
    return tuple(resolved)


async def _async_scheme_identifier(
    client: AuthenticatedGraphQLClient,
    grid_operator_code: str,
    product_code: str,
) -> str | None:
    """Return the time-of-use scheme `tariffSummary` reports for one product."""
    result = await client.execute_optional(
        TARIFF_SUMMARY_QUERY,
        {"gridOperatorCode": grid_operator_code, "productCode": product_code},
    )
    if result.data is None:
        return None
    summaries = result.data.get("tariffSummary")
    if not isinstance(summaries, list):
        return None
    for summary in summaries:
        if not isinstance(summary, Mapping):
            continue
        if _optional_string(summary.get("code")) != product_code:
            continue
        params = summary.get("productParams")
        if isinstance(params, Mapping):
            identifier = _optional_string(params.get("time_of_use_scheme"))
            if identifier is not None:
                return identifier
    return None


def parse_supply_point_tariffs(
    payload: Mapping[str, Any],
    account_number: str,
) -> tuple[SupplyPointTariff, ...]:
    """Parse the tariff response, skipping supply points with no usable agreement."""
    account = payload.get("account")
    if not isinstance(account, Mapping):
        raise OejpInvalidResponseError("Tariff response did not contain account")
    number = account.get("number")
    if isinstance(number, str) and number and number != account_number:
        raise OejpInvalidResponseError("Tariff response returned a different account")

    tariffs: list[SupplyPointTariff] = []
    for property_ in _iter_mappings(account.get("properties")):
        for point in _iter_mappings(property_.get("electricitySupplyPoints")):
            supply_point_id = _identifier(point)
            if supply_point_id is None:
                continue
            agreements = point.get("agreements")
            product = _active_product(agreements)
            if product is None:
                if _had_consumption_agreement(agreements):
                    tariffs.append(_lapsed(account_number, supply_point_id))
                continue
            tariffs.append(_parse_product(account_number, supply_point_id, product))
    return tuple(tariffs)


def _iter_mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _identifier(point: Mapping[str, Any]) -> str | None:
    for key in ("id", "spin"):
        value = point.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _had_consumption_agreement(agreements: object) -> bool:
    """Report whether this supply point ever had a consumption agreement.

    Separates a lapsed agreement from a point that never priced consumption at all. Revoked
    ones count: a revoked agreement is still evidence that this point is billed for what it
    consumes, and if it is the only one there is nothing in force now.
    """
    return any(
        isinstance(agreement.get("product"), Mapping)
        and _optional_string(agreement["product"].get("__typename")) in _CONSUMPTION_PRODUCTS
        for agreement in _iter_mappings(agreements)
    )


def _lapsed(account_number: str, supply_point_id: str) -> SupplyPointTariff:
    """Return a tariff that carries only the reason there is no price."""
    return SupplyPointTariff(
        account_number=account_number,
        supply_point_id=supply_point_id,
        product_code=None,
        product_name=None,
        steps=(),
        standing_charge_per_day=None,
        fuel_cost_adjustment=None,
        renewable_energy_levy=None,
        unpriceable_reason=TariffUnpriceable.AGREEMENT_LAPSED,
    )


def _active_product(agreements: object) -> Mapping[str, Any] | None:
    """Return the product of the consumption agreement in force, preferring the latest start.

    A revoked agreement is skipped. Among the rest the one that started most recently and
    has not ended wins, which is how a mid-period product switch resolves.

    Only a product that prices consumption is a candidate. `Product` is a union whose members
    include `ElectricityFitProduct` — export generation credits, no consumption charges — so
    choosing purely by start date let a later-starting export agreement hide the consumption
    tariff of an account that has both.
    """
    best: tuple[datetime, Mapping[str, Any]] | None = None
    for agreement in _iter_mappings(agreements):
        if agreement.get("isRevoked") is True:
            continue
        product = agreement.get("product")
        if not isinstance(product, Mapping):
            continue
        if _optional_string(product.get("__typename")) not in _CONSUMPTION_PRODUCTS:
            continue
        valid_to = _optional_datetime(agreement.get("validTo"))
        if valid_to is not None and valid_to <= datetime.now(tz=UTC):
            continue
        valid_from = _optional_datetime(agreement.get("validFrom")) or datetime.min.replace(
            tzinfo=UTC
        )
        if best is None or valid_from > best[0]:
            best = (valid_from, product)
    return best[1] if best is not None else None


def _parse_product(
    account_number: str,
    supply_point_id: str,
    product: Mapping[str, Any],
) -> SupplyPointTariff:
    """Build one supply point's tariff, or a tariff that records why it cannot be priced."""
    product_type = _optional_string(product.get("__typename"))
    charges = _iter_mappings(product.get("consumptionCharges"))

    def unpriceable(reason: TariffUnpriceable) -> SupplyPointTariff:
        return _tariff(
            account_number,
            supply_point_id,
            product,
            product_type,
            steps=(),
            reason=reason,
        )

    # `band` is not used to detect a stepped tariff: it differs per step there by design. One
    # real account returned `CONSUMPTION_STEPPED_03_01` through `_03` for its three steps.
    for key in ("gridOperatorCode", "regionOfOperation"):
        scopes = {_optional_string(charge.get(key)) for charge in charges} - {None}
        if len(scopes) > 1:
            return unpriceable(TariffUnpriceable.MIXED_OPERATOR)

    if any(_optional_string(charge.get("timeOfUse")) is not None for charge in charges):
        return _parse_time_of_use(account_number, supply_point_id, product, product_type, charges)

    steps: list[TariffStep] = []
    for charge in charges:
        unit = charge.get("unitType")
        if isinstance(unit, str) and unit != CONSUMPTION_UNIT:
            return unpriceable(TariffUnpriceable.UNSUPPORTED_UNIT)
        price = _optional_decimal(charge.get("pricePerUnitIncTax"))
        if price is None:
            continue
        start = _optional_decimal(charge.get("stepStart"))
        if start is None:
            if product_type != _SINGLE_STEP_PRODUCT:
                # A stepped charge that arrived without its boundary. Guessing one would
                # silently reprice every kWh above it.
                continue
            # `ConsumptionRate` has no step boundaries at all: a single-step product charges
            # one price for everything, which is the step from zero upwards.
            start = Decimal(0)
        steps.append(
            TariffStep(
                start_kwh=start,
                end_kwh=_optional_decimal(charge.get("stepEnd")),
                price_inc_tax=price,
                price_ex_tax=_optional_decimal(charge.get("pricePerUnit")),
                valid_from=_optional_datetime(charge.get("validFrom")),
                valid_to=_optional_datetime(charge.get("validTo")),
            )
        )
    if not steps:
        return unpriceable(TariffUnpriceable.NO_CONSUMPTION_CHARGES)
    steps.sort(key=lambda step: step.start_kwh)
    return _tariff(
        account_number,
        supply_point_id,
        product,
        product_type,
        steps=tuple(steps),
        reason=None,
    )


def _parse_time_of_use(
    account_number: str,
    supply_point_id: str,
    product: Mapping[str, Any],
    product_type: str | None,
    charges: list[Mapping[str, Any]],
) -> SupplyPointTariff:
    """Build a tariff priced by the hour rather than by cumulative consumption.

    The scheme identifier comes from `params`, which is the provider's own name for the
    schedule. It is left unresolved rather than refused when `params` does not carry it:
    `async_fetch_supply_point_tariffs` can still read it from `tariffSummary`, which answers
    for any product code without needing the account to be on it.
    """
    bands: list[TariffBand] = []
    areas: set[str] = set()
    grid_operator_code: str | None = None
    for charge in charges:
        unit = charge.get("unitType")
        if isinstance(unit, str) and unit != CONSUMPTION_UNIT:
            return _tariff(
                account_number,
                supply_point_id,
                product,
                product_type,
                steps=(),
                reason=TariffUnpriceable.UNSUPPORTED_UNIT,
            )
        price = _optional_decimal(charge.get("pricePerUnitIncTax"))
        band = _optional_string(charge.get("band"))
        split = split_band(band)
        if price is None or band is None or split is None:
            continue
        area, slot = split
        areas.add(area)
        grid_operator_code = _optional_string(charge.get("gridOperatorCode")) or area
        bands.append(
            TariffBand(
                slot=slot,
                band=band,
                price_inc_tax=price,
                price_ex_tax=_optional_decimal(charge.get("pricePerUnit")),
                valid_from=_optional_datetime(charge.get("validFrom")),
                valid_to=_optional_datetime(charge.get("validTo")),
            )
        )

    if len(areas) > 1:
        # The bands name two grid areas. The `gridOperatorCode` check upstream cannot see this,
        # because it only compares the charges that carry that field. It matters because one
        # scheme — オール電化オクトパス — has different hours in every area, so picking either
        # area's schedule would misprice the other's hours.
        return _tariff(
            account_number,
            supply_point_id,
            product,
            product_type,
            steps=(),
            reason=TariffUnpriceable.MIXED_OPERATOR,
        )

    tariff = _tariff(
        account_number,
        supply_point_id,
        product,
        product_type,
        steps=(),
        reason=None,
        bands=tuple(bands),
        tou_scheme=_time_of_use_scheme(product),
        grid_operator_code=grid_operator_code,
    )
    return resolve_time_of_use(tariff)


def _time_of_use_scheme(product: Mapping[str, Any]) -> str | None:
    """Read the scheme identifier out of the product's `params` blob."""
    params = product.get("params")
    if not isinstance(params, Mapping):
        return None
    return _optional_string(params.get("time_of_use_scheme"))


def resolve_time_of_use(
    tariff: SupplyPointTariff,
    scheme_identifier: str | None = None,
) -> SupplyPointTariff:
    """Check a time-of-use tariff against the transcribed schedule for its scheme.

    Called once while parsing, and again if the scheme identifier had to be fetched
    separately. Passing an identifier overrides the one already on the tariff.

    Refuses in three cases, each of which would otherwise produce a confident wrong cost:

    - the scheme is unnamed, or named something `tou.py` has no hours for;
    - the tariff names a grid area the scheme is not sold in; or
    - the agreement did not price every slot the scheme defines for that area, which would
      leave whole hours charged at nothing.
    """
    if not tariff.bands:
        return tariff
    identifier = scheme_identifier or tariff.tou_scheme
    scheme = scheme_for(identifier)
    area = (
        scheme.area(tariff.grid_operator_code)
        if scheme is not None and tariff.grid_operator_code is not None
        else None
    )
    if area is None:
        return replace(
            tariff,
            tou_scheme=identifier,
            unpriceable_reason=TariffUnpriceable.TIME_OF_USE_SCHEME_UNKNOWN,
        )
    if not area.slot_names <= {band.slot for band in tariff.bands}:
        return replace(
            tariff,
            tou_scheme=identifier,
            unpriceable_reason=TariffUnpriceable.TIME_OF_USE_BANDS_INCOMPLETE,
        )
    return replace(tariff, tou_scheme=identifier, unpriceable_reason=None)


def _tariff(
    account_number: str,
    supply_point_id: str,
    product: Mapping[str, Any],
    product_type: str | None,
    *,
    steps: tuple[TariffStep, ...],
    reason: TariffUnpriceable | None,
    bands: tuple[TariffBand, ...] = (),
    tou_scheme: str | None = None,
    grid_operator_code: str | None = None,
) -> SupplyPointTariff:
    return SupplyPointTariff(
        account_number=account_number,
        supply_point_id=supply_point_id,
        product_code=_optional_string(product.get("code")),
        product_name=_optional_string(product.get("displayName")),
        steps=steps,
        standing_charge_per_day=_optional_decimal(product.get("standingChargePricePerDay")),
        fuel_cost_adjustment=_parse_adder(product.get("fuelCostAdjustment")),
        renewable_energy_levy=_parse_adder(product.get("renewableEnergyLevy")),
        standing_charge_unit=_optional_string(product.get("standingChargeUnitType")),
        product_type=product_type,
        unpriceable_reason=reason,
        bands=bands,
        tou_scheme=tou_scheme,
        grid_operator_code=grid_operator_code,
    )


def _parse_adder(value: object) -> TariffAdder | None:
    if not isinstance(value, Mapping):
        return None
    unit = value.get("unitType")
    if isinstance(unit, str) and unit != CONSUMPTION_UNIT:
        return None
    price = _optional_decimal(value.get("pricePerUnitIncTax"))
    if price is None:
        return None
    stated_from, stated_to = value.get("validFrom"), value.get("validTo")
    valid_from = _optional_datetime(stated_from)
    valid_to = _optional_datetime(stated_to)
    if (stated_from is not None and valid_from is None) or (
        stated_to is not None and valid_to is None
    ):
        # A bound the provider stated and this cannot read. An absent bound means open-ended,
        # so treating an unreadable one the same way would produce an adder that applies to
        # every moment in history — and it would be archived under no period at all.
        return None
    return TariffAdder(
        price_inc_tax=price,
        price_ex_tax=_optional_decimal(value.get("pricePerUnit")),
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _optional_string(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    return parsed if parsed.is_finite() else None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
