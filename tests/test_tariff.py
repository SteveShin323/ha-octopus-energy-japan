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
    parse_supply_point_tariffs,
)

ACCOUNT = "A-1"


def _product(**overrides: Any) -> dict[str, Any]:
    product = {
        "__typename": "ElectricitySteppedProduct",
        "code": "JPN_KK_OCTOPUS_MAY_26",
        "displayName": "KKオクトパス",
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

    assert tariff.marginal_price(cumulative) == Decimal(expected)


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


def test_a_single_step_product_has_no_steps_and_is_not_priceable() -> None:
    """A flat-rate product carries no `consumptionCharges` on this type.

    Returning it as unpriceable is deliberate: inventing a step from the standing charge
    would produce a cost with no basis.
    """
    product = {
        "__typename": "ElectricitySingleStepProduct",
        "code": "FLAT",
        "displayName": "Flat",
        "standingChargeUnitType": "YEN_AMPERE_DAY",
        "standingChargePricePerDay": "30.00",
        "fuelCostAdjustment": None,
        "renewableEnergyLevy": None,
    }
    (tariff,) = parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT)

    assert not tariff.is_priceable
    assert tariff.standing_charge_per_day == Decimal("30.00")


def test_a_charge_in_a_unit_this_formula_cannot_price_drops_the_tariff() -> None:
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

    assert parse_supply_point_tariffs(_payload(_agreement(product=product)), ACCOUNT) == ()


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
    assert tariff.marginal_price(Decimal(0)) is None
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


def test_a_malformed_timestamp_is_ignored_rather_than_failing_the_read() -> None:
    product = _product(
        renewableEnergyLevy={
            "pricePerUnitIncTax": "4.18",
            "unitType": "KWH_CONSUMPTION",
            "validFrom": "not a timestamp",
            "validTo": None,
        }
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
