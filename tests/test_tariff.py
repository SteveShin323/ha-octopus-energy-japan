"""Tests for reading the tariff off the agreement's product.

The response shape here is copied from a real account on 2026-08-04, with the customer's
identifiers replaced. The prices are the published Tokyo tariff, which is public.

Three earlier attempts concluded this data was unreachable, each by searching for a field
whose declared type was `ProductInterface` or a member of it. The declared type is the
union `Product`. That is why `test_the_query_reaches_the_product_through_an_inline_fragment`
exists: it pins the one structural fact that made the difference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from custom_components.octopus_energy_japan.api import OejpInvalidResponseError
from custom_components.octopus_energy_japan.api.tariff import (
    SUPPLY_POINT_TARIFF_QUERY,
    SupplyPointTariff,
    TariffAdder,
    TariffStep,
    TariffUnpriceable,
    parse_supply_point_tariffs,
)

NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)
ACCOUNT = "A-1"


def _product(**overrides: Any) -> dict[str, Any]:
    product = {
        "__typename": "ElectricitySteppedProduct",
        "code": "JPN_KK_OCTOPUS_MAY_26",
        "displayName": "Demo Standard Plan",
        "standingChargeUnitType": "YEN_AMPERE_DAY",
        "standingChargePricePerDay": "38.80",
        "consumptionCharges": [
            {
                "stepStart": 0,
                "stepEnd": 120,
                "pricePerUnit": "18.74545",
                "pricePerUnitIncTax": "20.62",
                "unitType": "KWH_CONSUMPTION",
                "validFrom": "2026-03-31T15:00:00+00:00",
                "validTo": None,
            },
            {
                "stepStart": 120,
                "stepEnd": 300,
                "pricePerUnit": "22.99091",
                "pricePerUnitIncTax": "25.29",
                "unitType": "KWH_CONSUMPTION",
                "validFrom": "2026-03-31T15:00:00+00:00",
                "validTo": None,
            },
            {
                "stepStart": 300,
                "stepEnd": None,
                "pricePerUnit": "24.94545",
                "pricePerUnitIncTax": "27.44",
                "unitType": "KWH_CONSUMPTION",
                "validFrom": "2026-03-31T15:00:00+00:00",
                "validTo": None,
            },
        ],
        "fuelCostAdjustment": {
            "pricePerUnit": "3.92727",
            "pricePerUnitIncTax": "4.32",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": "2026-07-31T15:00:00+00:00",
            "validTo": "2026-08-31T15:00:00+00:00",
        },
        "renewableEnergyLevy": {
            "pricePerUnit": "3.8",
            "pricePerUnitIncTax": "4.18",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": "2026-04-30T15:00:00+00:00",
            "validTo": "2027-04-30T15:00:00+00:00",
        },
    }
    product.update(overrides)
    return product


def _payload(*agreements: dict[str, Any], number: str = ACCOUNT) -> dict[str, Any]:
    return {
        "account": {
            "number": number,
            "properties": [
                {
                    "electricitySupplyPoints": [
                        {"id": "SP-1", "spin": "0" * 22, "agreements": list(agreements)}
                    ]
                }
            ],
        }
    }


def _agreement(**overrides: Any) -> dict[str, Any]:
    agreement = {
        "id": 1,
        "validFrom": "2026-06-17T15:00:00+00:00",
        "validTo": None,
        "isRevoked": False,
        "product": _product(),
    }
    agreement.update(overrides)
    return agreement


def test_the_query_reaches_the_product_through_an_inline_fragment() -> None:
    """`Agreement.product` is the union `Product`, not one of its members.

    Searching the schema for a field returning `ElectricitySteppedProduct` finds nothing,
    which is how three earlier passes concluded this data did not exist. The fragment is
    the whole difference.
    """
    assert "... on ElectricitySteppedProduct" in SUPPLY_POINT_TARIFF_QUERY
    assert "... on ElectricitySingleStepProduct" in SUPPLY_POINT_TARIFF_QUERY
    # Read through `agreements`, not `marketSupplyAgreements`: the latter's `product.rates`
    # is refused and the refusal nulls the product with it.
    assert "electricitySupplyPoints" in SUPPLY_POINT_TARIFF_QUERY
    assert "marketSupplyAgreements" not in SUPPLY_POINT_TARIFF_QUERY
    for field in (
        "consumptionCharges",
        "standingChargePricePerDay",
        "fuelCostAdjustment",
        "renewableEnergyLevy",
        "pricePerUnitIncTax",
    ):
        assert field in SUPPLY_POINT_TARIFF_QUERY


def test_a_real_response_parses_into_a_priceable_tariff() -> None:
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement()), ACCOUNT)

    assert tariff.is_priceable
    assert tariff.product_code == "JPN_KK_OCTOPUS_MAY_26"
    assert tariff.standing_charge_per_day == Decimal("38.80")
    assert [step.start_kwh for step in tariff.steps] == [Decimal(0), Decimal(120), Decimal(300)]
    assert [step.price_inc_tax for step in tariff.steps] == [
        Decimal("20.62"),
        Decimal("25.29"),
        Decimal("27.44"),
    ]
    # The net prices are kept, so a future tax-exclusive presentation needs no refetch.
    assert tariff.steps[0].price_ex_tax == Decimal("18.74545")


@pytest.mark.parametrize(
    ("cumulative", "expected"),
    [
        (Decimal(0), "20.62"),
        (Decimal("119.9"), "20.62"),
        (Decimal(120), "25.29"),
        (Decimal("299.9"), "25.29"),
        (Decimal(300), "27.44"),
        (Decimal(5000), "27.44"),
    ],
)
def test_the_marginal_price_steps_at_each_boundary(cumulative: Decimal, expected: str) -> None:
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement()), ACCOUNT)

    assert tariff.marginal_price(cumulative, NOW) == Decimal(expected)


def test_the_adders_apply_only_inside_the_period_the_provider_states() -> None:
    """The fuel adjustment is monthly, so an older hour must not be priced with it.

    Using the current month's adjustment for a previous month would be a silent error of
    a few yen per kWh in whichever direction fuel costs moved.
    """
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement()), ACCOUNT)

    inside = datetime(2026, 8, 4, tzinfo=UTC)
    before = datetime(2026, 7, 1, tzinfo=UTC)
    after = datetime(2026, 10, 1, tzinfo=UTC)

    # Both apply in August: 4.32 fuel adjustment plus 4.18 levy.
    assert tariff.adders_at(inside) == Decimal("8.50")
    # In July only the levy is in force.
    assert tariff.adders_at(before) == Decimal("4.18")
    # In October the fuel adjustment has expired and the levy still runs to next April.
    assert tariff.adders_at(after) == Decimal("4.18")


def test_an_adder_without_stated_bounds_is_treated_as_current() -> None:
    product = _product(
        fuelCostAdjustment={
            "pricePerUnitIncTax": "1.00",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": None,
            "validTo": None,
        }
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.adders_at(datetime(2030, 1, 1, tzinfo=UTC)) >= Decimal("1.00")


def test_a_revoked_agreement_is_ignored() -> None:
    revoked = _agreement(isRevoked=True, product=_product(code="OLD"))
    current = _agreement(id=2, product=_product(code="NEW"))

    (tariff,) = parse_supply_point_tariffs(_payload(revoked, current), ACCOUNT)

    assert tariff.product_code == "NEW"


def test_an_ended_agreement_is_ignored() -> None:
    ended = _agreement(
        validTo="2026-01-01T00:00:00+00:00",
        product=_product(code="ENDED"),
    )
    current = _agreement(id=2, product=_product(code="CURRENT"))

    (tariff,) = parse_supply_point_tariffs(_payload(ended, current), ACCOUNT)

    assert tariff.product_code == "CURRENT"


def test_the_latest_starting_agreement_wins_a_mid_period_switch() -> None:
    older = _agreement(validFrom="2026-01-01T00:00:00+00:00", product=_product(code="OLDER"))
    newer = _agreement(
        id=2,
        validFrom="2026-06-17T15:00:00+00:00",
        product=_product(code="NEWER"),
    )

    (tariff,) = parse_supply_point_tariffs(_payload(older, newer), ACCOUNT)

    assert tariff.product_code == "NEWER"


def _single_step_product(**overrides: Any) -> dict[str, Any]:
    product = {
        "__typename": "ElectricitySingleStepProduct",
        "code": "FLAT",
        "displayName": "Flat",
        "standingChargeUnitType": "YEN_AMPERE_DAY",
        "standingChargePricePerDay": "30.00",
        "consumptionCharges": [
            {
                "pricePerUnit": "27.00",
                "pricePerUnitIncTax": "29.70",
                "unitType": "KWH_CONSUMPTION",
                "timeOfUse": None,
                "gridOperatorCode": "03",
                "regionOfOperation": None,
                "validFrom": "2026-03-31T15:00:00+00:00",
                "validTo": None,
            }
        ],
        "fuelCostAdjustment": None,
        "renewableEnergyLevy": None,
    }
    product.update(overrides)
    return product


def test_a_single_step_product_is_priced_from_its_one_charge() -> None:
    """`ConsumptionRate` has no step boundaries, which is not the same as having no price.

    The query used to select `consumptionCharges` only on the stepped product, so every
    account on a flat-rate plan got no cost statistic at all and no explanation. Introspection
    shows `ElectricitySingleStepProduct.consumptionCharges` exists.
    """
    (tariff,) = parse_supply_point_tariffs(
        _payload(_agreement(product=_single_step_product())),
        ACCOUNT,
    )

    assert tariff.is_priceable
    assert tariff.unpriceable_reason is None
    assert len(tariff.steps) == 1
    # One price for everything: the step runs from zero with no upper bound.
    assert tariff.steps[0].start_kwh == Decimal(0)
    assert tariff.steps[0].end_kwh is None
    assert tariff.marginal_price(Decimal(10_000), NOW) == Decimal("29.70")


def test_a_consumption_product_with_no_usable_charge_records_why() -> None:
    (tariff,) = parse_supply_point_tariffs(
        _payload(_agreement(product=_single_step_product(consumptionCharges=[]))),
        ACCOUNT,
    )

    assert not tariff.is_priceable
    assert tariff.unpriceable_reason is TariffUnpriceable.NO_CONSUMPTION_CHARGES
    assert tariff.standing_charge_per_day == Decimal("30.00")


def test_a_stepped_charge_without_its_boundary_is_dropped_not_guessed() -> None:
    """Only the single-step type legitimately omits the boundary."""
    product = _product(
        consumptionCharges=[
            {
                "stepStart": None,
                "stepEnd": None,
                "pricePerUnitIncTax": "20.62",
                "unitType": "KWH_CONSUMPTION",
            }
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.unpriceable_reason is TariffUnpriceable.NO_CONSUMPTION_CHARGES


def test_a_time_of_use_rate_makes_the_tariff_unusable() -> None:
    """This formula prices by cumulative consumption alone.

    Both rate types carry `timeOfUse`. Treating rates that differ by time of day as steps
    would misprice every hour while looking like a working cost.
    """
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": None,
                "pricePerUnitIncTax": "20.62",
                "unitType": "KWH_CONSUMPTION",
                "timeOfUse": "NIGHT",
            }
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.unpriceable_reason is TariffUnpriceable.TIME_OF_USE


@pytest.mark.parametrize("key", ["gridOperatorCode", "regionOfOperation"])
def test_charges_from_more_than_one_operator_make_the_tariff_unusable(key: str) -> None:
    """Two operators' rates cannot be one step ladder."""
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": 120,
                "pricePerUnitIncTax": "20.62",
                "unitType": "KWH_CONSUMPTION",
                key: "03",
            },
            {
                "stepStart": 120,
                "stepEnd": None,
                "pricePerUnitIncTax": "25.29",
                "unitType": "KWH_CONSUMPTION",
                key: "04",
            },
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.unpriceable_reason is TariffUnpriceable.MIXED_OPERATOR


def test_a_band_that_differs_per_step_is_not_a_conflict() -> None:
    """One real account returned `CONSUMPTION_STEPPED_03_01` through `_03` for its steps.

    `band` names the step, so refusing on it would refuse every stepped tariff.
    """
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": 120,
                "pricePerUnitIncTax": "20.62",
                "unitType": "KWH_CONSUMPTION",
                "band": "CONSUMPTION_STEPPED_03_01",
                "gridOperatorCode": "03",
            },
            {
                "stepStart": 120,
                "stepEnd": None,
                "pricePerUnitIncTax": "25.29",
                "unitType": "KWH_CONSUMPTION",
                "band": "CONSUMPTION_STEPPED_03_02",
                "gridOperatorCode": "03",
            },
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.is_priceable
    assert len(tariff.steps) == 2


def test_an_export_agreement_does_not_hide_the_consumption_tariff() -> None:
    """`ElectricityFitProduct` is a union member with generation credits and no charges.

    Choosing the agreement with the latest start regardless of what it prices lost the
    consumption tariff of an account that also exports.
    """
    fit = {
        "__typename": "ElectricityFitProduct",
        "code": "FIT",
        "displayName": "FIT",
    }
    tariffs = parse_supply_point_tariffs(
        _payload(
            _agreement(product=_product(), validFrom="2026-03-31T15:00:00+00:00"),
            _agreement(product=fit, validFrom="2026-06-30T15:00:00+00:00"),
        ),
        ACCOUNT,
    )

    assert len(tariffs) == 1
    assert tariffs[0].is_priceable
    assert tariffs[0].product_type == "ElectricitySteppedProduct"


def test_the_standing_charge_unit_and_product_type_are_carried_not_acted_on() -> None:
    """One real account reported `YEN_AMPERE_DAY`, so the value set is not known.

    An allow-list built from one account would refuse valid tariffs, so the value is reported
    for diagnosis instead of gating the calculation.
    """
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement()), ACCOUNT)

    assert tariff.standing_charge_unit == "YEN_AMPERE_DAY"
    assert tariff.product_type == "ElectricitySteppedProduct"
    assert tariff.is_priceable


def test_a_charge_in_a_unit_this_formula_cannot_price_records_why() -> None:
    """A capacity or demand charge needs a different formula.

    Pricing the per-kWh part of such a tariff and ignoring the rest would look like a
    working cost while being wrong by however much the other component is.
    """
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": None,
                "pricePerUnitIncTax": "100",
                "unitType": "KVA_DEMAND_MONTHS",
            }
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert not tariff.is_priceable
    assert tariff.unpriceable_reason is TariffUnpriceable.UNSUPPORTED_UNIT


def test_a_supply_point_with_no_agreement_is_skipped() -> None:
    assert parse_supply_point_tariffs(_payload(), ACCOUNT) == ()


def test_a_response_for_another_account_is_rejected() -> None:
    with pytest.raises(OejpInvalidResponseError, match="different account"):
        parse_supply_point_tariffs(_payload(_agreement(), number="SOMEONE-ELSE"), ACCOUNT)


def test_a_response_without_an_account_is_rejected() -> None:
    with pytest.raises(OejpInvalidResponseError, match="did not contain account"):
        parse_supply_point_tariffs({}, ACCOUNT)


@pytest.mark.parametrize(
    "price",
    ["not a number", None, True, float("nan")],
    ids=["text", "null", "bool", "nan"],
)
def test_an_unusable_price_is_dropped_rather_than_guessed(price: object) -> None:
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": None,
                "pricePerUnitIncTax": price,
                "unitType": "KWH_CONSUMPTION",
            }
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert not tariff.is_priceable


def test_steps_are_sorted_however_the_provider_orders_them() -> None:
    product = _product()
    product["consumptionCharges"] = list(reversed(product["consumptionCharges"]))  # type: ignore[arg-type]

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert [step.start_kwh for step in tariff.steps] == [Decimal(0), Decimal(120), Decimal(300)]


def test_a_step_contains_its_start_but_not_its_end() -> None:
    step = TariffStep(start_kwh=Decimal(120), end_kwh=Decimal(300), price_inc_tax=Decimal(1))

    assert not step.contains(Decimal("119.999"))
    assert step.contains(Decimal(120))
    assert step.contains(Decimal("299.999"))
    assert not step.contains(Decimal(300))


def test_an_unbounded_step_has_no_upper_limit() -> None:
    step = TariffStep(start_kwh=Decimal(300), end_kwh=None, price_inc_tax=Decimal(1))

    assert step.contains(Decimal(300))
    assert step.contains(Decimal(1_000_000))


def test_a_tariff_with_no_steps_prices_nothing() -> None:
    tariff = SupplyPointTariff(
        account_number=ACCOUNT,
        supply_point_id="SP-1",
        product_code=None,
        product_name=None,
        steps=(),
        standing_charge_per_day=None,
        fuel_cost_adjustment=None,
        renewable_energy_levy=None,
    )

    assert not tariff.is_priceable
    assert tariff.marginal_price(Decimal(0), NOW) is None
    assert tariff.adders_at(datetime(2026, 8, 4, tzinfo=UTC)) == Decimal(0)


def test_an_adder_in_an_unpriceable_unit_is_ignored() -> None:
    assert (
        TariffAdder(price_inc_tax=Decimal(1)).applies_at(datetime(2026, 8, 4, tzinfo=UTC)) is True
    )
    product = _product(
        renewableEnergyLevy={
            "pricePerUnitIncTax": "4.18",
            "unitType": "MONTHS_ON_SUPPLY",
        }
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.renewable_energy_levy is None


async def test_the_fetch_helper_returns_nothing_when_the_query_is_refused() -> None:
    """A refused optional read must not fail the refresh that carries it."""
    from unittest.mock import AsyncMock

    from custom_components.octopus_energy_japan.api import GraphQLResult
    from custom_components.octopus_energy_japan.api.tariff import (
        async_fetch_supply_point_tariffs,
    )

    client = AsyncMock()
    client.execute_optional = AsyncMock(return_value=GraphQLResult(data=None, errors=()))

    assert await async_fetch_supply_point_tariffs(client, ACCOUNT) == ()


async def test_the_fetch_helper_parses_a_successful_read() -> None:
    from unittest.mock import AsyncMock

    from custom_components.octopus_energy_japan.api import GraphQLResult
    from custom_components.octopus_energy_japan.api.tariff import (
        async_fetch_supply_point_tariffs,
    )

    client = AsyncMock()
    client.execute_optional = AsyncMock(
        return_value=GraphQLResult(data=_payload(_agreement()), errors=())
    )

    (tariff,) = await async_fetch_supply_point_tariffs(client, ACCOUNT)

    assert tariff.product_code == "JPN_KK_OCTOPUS_MAY_26"


def test_a_supply_point_with_no_identifier_is_skipped() -> None:
    payload = {
        "account": {
            "number": ACCOUNT,
            "properties": [
                {"electricitySupplyPoints": [{"id": "", "spin": "  ", "agreements": []}]}
            ],
        }
    }

    assert parse_supply_point_tariffs(payload, ACCOUNT) == ()


def test_a_non_mapping_agreement_or_product_is_skipped() -> None:
    payload = _payload({"id": 1, "product": "not a mapping", "isRevoked": False})

    assert parse_supply_point_tariffs(payload, ACCOUNT) == ()


def test_a_charge_without_a_step_start_is_skipped_not_guessed() -> None:
    product = _product(
        consumptionCharges=[
            {"stepStart": None, "stepEnd": 120, "pricePerUnitIncTax": "20.62"},
            {"stepStart": 120, "stepEnd": None, "pricePerUnitIncTax": "25.29"},
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert [step.start_kwh for step in tariff.steps] == [Decimal(120)]


def test_an_adder_whose_stated_bound_cannot_be_read_is_refused() -> None:
    """An absent bound means open-ended, so an unreadable one must not be read that way.

    Treating them alike produced an adder that applied to every moment in history, and the
    archive of past adjustments would have filed it under no period at all.
    """
    product = _product(
        renewableEnergyLevy={
            "pricePerUnitIncTax": "4.18",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": "not a timestamp",
            "validTo": None,
        }
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.renewable_energy_levy is None


def test_an_adder_with_no_stated_bounds_is_still_open_ended() -> None:
    """The provider omits the bound on rates it considers current."""
    product = _product(
        renewableEnergyLevy={"pricePerUnitIncTax": "4.18", "unitType": "KWH_CONSUMPTION"}
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.renewable_energy_levy is not None
    assert tariff.renewable_energy_levy.valid_from is None


def test_a_naive_timestamp_is_read_as_utc() -> None:
    product = _product(
        renewableEnergyLevy={
            "pricePerUnitIncTax": "4.18",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": "2026-04-30T15:00:00",
            "validTo": None,
        }
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.renewable_energy_levy is not None
    assert tariff.renewable_energy_levy.valid_from == datetime(2026, 4, 30, 15, tzinfo=UTC)


def test_an_adder_with_no_price_is_dropped() -> None:
    product = _product(fuelCostAdjustment={"unitType": "KWH_CONSUMPTION"})
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.fuel_cost_adjustment is None


def test_properties_that_are_not_lists_are_ignored() -> None:
    payload = {"account": {"number": ACCOUNT, "properties": "not a list"}}

    assert parse_supply_point_tariffs(payload, ACCOUNT) == ()


def _generation(start: str | None, end: str | None, price: str) -> list[dict[str, Any]]:
    return [
        {
            "stepStart": 0,
            "stepEnd": 120,
            "pricePerUnitIncTax": price,
            "unitType": "KWH_CONSUMPTION",
            "validFrom": start,
            "validTo": end,
        },
        {
            "stepStart": 120,
            "stepEnd": None,
            "pricePerUnitIncTax": price,
            "unitType": "KWH_CONSUMPTION",
            "validFrom": start,
            "validTo": end,
        },
    ]


def _two_generation_tariff() -> SupplyPointTariff:
    product = _product(
        consumptionCharges=[
            *_generation("2026-01-31T15:00:00+00:00", "2026-06-30T15:00:00+00:00", "10.00"),
            *_generation("2026-06-30T15:00:00+00:00", None, "20.00"),
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)
    return tariff


def test_an_hour_is_priced_with_the_rates_in_force_then() -> None:
    """Two generations of rates must not be merged into one ladder.

    Merged, the boundaries repeat and which price applies depends on sort order rather than on
    the date, so an hour from either generation could be priced with the other's rates.
    """
    tariff = _two_generation_tariff()

    old = datetime(2026, 3, 1, tzinfo=UTC)
    new = datetime(2026, 8, 1, tzinfo=UTC)

    assert tariff.marginal_price(Decimal(0), old) == Decimal("10.00")
    assert tariff.marginal_price(Decimal(0), new) == Decimal("20.00")
    assert len(tariff.steps_at(old)) == 2
    assert len(tariff.steps_at(new)) == 2


def test_an_hour_no_generation_covers_uses_the_nearest_one() -> None:
    """The provider serves the rates it currently publishes, not every historical one.

    Refusing to price an hour outside the published windows would leave holes in the cost
    series, and reaching across the whole range would be a larger error than reaching to the
    near end of it. Same rule as the stored fuel-cost adjustments.
    """
    tariff = _two_generation_tariff()

    before_everything = datetime(2025, 1, 1, tzinfo=UTC)
    assert tariff.marginal_price(Decimal(0), before_everything) == Decimal("10.00")


def test_one_generation_of_rates_is_the_whole_ladder() -> None:
    """The common case must not change: a single window is used for every hour."""
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement()), ACCOUNT)

    assert tariff.steps_at(datetime(2020, 1, 1, tzinfo=UTC)) == tariff.steps
    assert tariff.steps_at(datetime(2030, 1, 1, tzinfo=UTC)) == tariff.steps


def test_a_generation_with_no_stated_start_sorts_before_a_dated_one() -> None:
    product = _product(
        consumptionCharges=[
            *_generation(None, "2026-06-30T15:00:00+00:00", "10.00"),
            *_generation("2026-06-30T15:00:00+00:00", None, "20.00"),
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.marginal_price(Decimal(0), datetime(2020, 1, 1, tzinfo=UTC)) == Decimal("10.00")
    assert tariff.marginal_price(Decimal(0), datetime(2026, 8, 1, tzinfo=UTC)) == Decimal("20.00")


def test_an_earlier_second_agreement_does_not_replace_the_one_in_force() -> None:
    """Selection is by latest start, so order in the response must not decide it."""
    tariffs = parse_supply_point_tariffs(
        _payload(
            _agreement(product=_product(code="NEWER"), validFrom="2026-06-30T15:00:00+00:00"),
            _agreement(product=_product(code="OLDER"), validFrom="2026-01-31T15:00:00+00:00"),
        ),
        ACCOUNT,
    )

    assert [tariff.product_code for tariff in tariffs] == ["NEWER"]


@pytest.mark.parametrize("value", [[], {}, ()])
def test_a_price_that_is_not_a_scalar_is_dropped(value: object) -> None:
    product = _product(
        consumptionCharges=[
            {
                "stepStart": 0,
                "stepEnd": None,
                "pricePerUnitIncTax": value,
                "unitType": "KWH_CONSUMPTION",
            }
        ]
    )

    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.unpriceable_reason is TariffUnpriceable.NO_CONSUMPTION_CHARGES


def test_an_hour_in_a_gap_between_generations_keeps_the_last_price_in_force() -> None:
    """A gap means no published rate, not that a future rate applied.

    Reaching past the gap for the later generation would price an hour with prices that had not
    been announced yet. Carrying the last one that had begun forward is the smaller error.
    """
    product = _product(
        consumptionCharges=[
            *_generation("2026-01-31T15:00:00+00:00", "2026-03-31T15:00:00+00:00", "10.00"),
            *_generation("2026-06-30T15:00:00+00:00", None, "20.00"),
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    in_the_gap = datetime(2026, 5, 1, tzinfo=UTC)

    assert tariff.marginal_price(Decimal(0), in_the_gap) == Decimal("10.00")
    # Before either generation began, the earliest is still the nearest thing to the hour.
    assert tariff.marginal_price(Decimal(0), datetime(2025, 1, 1, tzinfo=UTC)) == Decimal("10.00")
    # And after the later one began, it applies.
    assert tariff.marginal_price(Decimal(0), datetime(2026, 8, 1, tzinfo=UTC)) == Decimal("20.00")


def test_an_hour_after_every_generation_ended_keeps_the_last_one() -> None:
    """Every published rate can carry an end date, leaving later hours uncovered."""
    product = _product(
        consumptionCharges=[
            *_generation("2026-01-31T15:00:00+00:00", "2026-03-31T15:00:00+00:00", "10.00"),
            *_generation("2026-03-31T15:00:00+00:00", "2026-06-30T15:00:00+00:00", "20.00"),
        ]
    )
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert tariff.marginal_price(Decimal(0), datetime(2026, 8, 1, tzinfo=UTC)) == Decimal("20.00")


def test_a_lapsed_agreement_is_reported_rather_than_silently_priceless() -> None:
    """Every consumption agreement has ended and nothing replaced it.

    A plan switch, or a move-out with the entry still installed. The cost statistic stops
    either way; before this it stopped in silence, because the only other supply point that
    publishes no cost for a structural reason is an export-only one, which is silent on
    purpose.
    """
    tariffs = parse_supply_point_tariffs(
        {
            "account": {
                "number": "A-1",
                "properties": [
                    {
                        "electricitySupplyPoints": [
                            {
                                "id": "SP-1",
                                "agreements": [
                                    {
                                        "validFrom": "2025-04-01T00:00:00+00:00",
                                        "validTo": "2026-03-31T00:00:00+00:00",
                                        "isRevoked": False,
                                        "product": {
                                            "__typename": "ElectricitySteppedProduct",
                                            "code": "P",
                                            "displayName": "P",
                                            "consumptionCharges": [],
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        },
        "A-1",
    )

    assert len(tariffs) == 1
    assert tariffs[0].unpriceable_reason is TariffUnpriceable.AGREEMENT_LAPSED
    assert not tariffs[0].is_priceable


def test_a_point_that_never_priced_consumption_stays_silent() -> None:
    """An export-only point is not a problem to report, and must not raise a repair issue."""
    tariffs = parse_supply_point_tariffs(
        {
            "account": {
                "number": "A-1",
                "properties": [
                    {
                        "electricitySupplyPoints": [
                            {
                                "id": "SP-1",
                                "agreements": [
                                    {
                                        "validFrom": "2025-04-01T00:00:00+00:00",
                                        "validTo": None,
                                        "isRevoked": False,
                                        "product": {
                                            "__typename": "ElectricityFitProduct",
                                            "code": "FIT",
                                            "displayName": "FIT",
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        },
        "A-1",
    )

    assert tariffs == ()
