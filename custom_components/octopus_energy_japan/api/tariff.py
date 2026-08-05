"""The tariff a supply point is actually charged on.

Everything a Japanese electricity bill is built from is readable by an account user, on
the agreement's product. Confirmed field by field against one real account on 2026-08-04
and recorded in `docs/API_CONTRACTS.md`:

- `consumptionCharges` — the energy price. On `ElectricitySteppedProduct` each entry carries
  its kWh step boundaries; on `ElectricitySingleStepProduct` there are none, because one
  price covers everything;
- `standingChargePricePerDay` — a per-day figure. `standingChargeUnitType` says what the
  charge is really measured in, and on the one account observed it was `YEN_AMPERE_DAY`
  rather than a per-day unit, so whether the figure is already resolved for the customer's
  contracted amperage is **not** established. It is used as a daily amount and the unit is
  reported in diagnostics so a mismatch can be traced rather than guessed at;
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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from .auth import AuthenticatedGraphQLClient
from .errors import OejpInvalidResponseError

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
                standingChargeUnitType
                standingChargePricePerDay
                consumptionCharges {
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
                standingChargeUnitType
                standingChargePricePerDay
                consumptionCharges {
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
    # A rate that varies by time of day. This formula prices by cumulative consumption alone,
    # so treating those rates as steps would misprice every hour.
    TIME_OF_USE = "time_of_use"
    # Charges from more than one grid operator or region, which cannot be one step ladder.
    MIXED_OPERATOR = "mixed_operator"
    # A consumption product that returned no usable charge.
    NO_CONSUMPTION_CHARGES = "no_consumption_charges"


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

    @property
    def is_priceable(self) -> bool:
        """Report whether a cost can be computed at all.

        Steps are the irreducible part: without them there is no energy price. The
        standing charge and the two adders are each optional, and their absence lowers the
        result rather than invalidating it, which the projector records.
        """
        return bool(self.steps)

    def steps_at(self, moment: datetime) -> tuple[TariffStep, ...]:
        """Return the step ladder the provider says was in force at ``moment``.

        The provider may publish more than one generation of rates, each with its own validity
        window. Merging them builds one ladder out of two tariffs: the boundaries repeat and
        which price applies depends on sort order rather than on the date. Selecting the
        generation that covers the moment prices each hour with the rates that applied then.

        A moment no generation covers is priced with the nearest one — the earliest for an hour
        before the first window, the latest for an hour after the last. Refusing to price those
        hours would leave holes in the cost series, and reaching across the whole range for a
        price is a larger error than reaching to the near end of it. This is the same rule the
        stored fuel-cost adjustments follow.
        """
        windows: dict[tuple[datetime | None, datetime | None], list[TariffStep]] = {}
        for step in self.steps:
            windows.setdefault((step.valid_from, step.valid_to), []).append(step)
        if len(windows) <= 1:
            return self.steps

        ordered = sorted(windows.items(), key=lambda item: item[0][0] or _DISTANT_PAST)
        for (valid_from, valid_to), steps in ordered:
            covers_start = valid_from is None or valid_from <= moment
            covers_end = valid_to is None or moment < valid_to
            if covers_start and covers_end:
                return tuple(steps)
        earliest_start = ordered[0][0][0]
        nearest = (
            ordered[0] if earliest_start is not None and moment < earliest_start else ordered[-1]
        )
        return tuple(nearest[1])

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
    return parse_supply_point_tariffs(result.data, account_number)


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
            product = _active_product(point.get("agreements"))
            if product is None:
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

    if any(_optional_string(charge.get("timeOfUse")) is not None for charge in charges):
        return unpriceable(TariffUnpriceable.TIME_OF_USE)
    # `band` is deliberately not checked: it differs per step by design. One real account
    # returned `CONSUMPTION_STEPPED_03_01` through `_03` for its three steps.
    for key in ("gridOperatorCode", "regionOfOperation"):
        scopes = {_optional_string(charge.get(key)) for charge in charges} - {None}
        if len(scopes) > 1:
            return unpriceable(TariffUnpriceable.MIXED_OPERATOR)

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


def _tariff(
    account_number: str,
    supply_point_id: str,
    product: Mapping[str, Any],
    product_type: str | None,
    *,
    steps: tuple[TariffStep, ...],
    reason: TariffUnpriceable | None,
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
    return TariffAdder(
        price_inc_tax=price,
        price_ex_tax=_optional_decimal(value.get("pricePerUnit")),
        valid_from=_optional_datetime(value.get("validFrom")),
        valid_to=_optional_datetime(value.get("validTo")),
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
